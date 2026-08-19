"""The production stages, end to end against the database.

Rendering needs a local ffmpeg. That is a tool, not a network service -- the
suite still never touches the internet -- but it is skipped when absent so the
rest of the pipeline stays testable on a machine without it.

TTS runs through the mock provider throughout. It writes real WAV files of the
correct length, which is exactly what the timing, caption and muxing logic needs
in order to be exercised honestly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pulpmill.captions.cues import group_into_cues
from pulpmill.config.models import AppConfig
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.domain.errors import ScriptError, StoryTooLongError
from pulpmill.pipeline.context import Application
from pulpmill.pipeline.production import ProductionRunner
from pulpmill.scripting.service import ScriptBuilder
from pulpmill.tts.service import NarrationSynthesizer, build_tts_provider

pytestmark = pytest.mark.usefixtures("database")

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


@pytest.fixture
def selected_story(app: Application, make_story):  # type: ignore[no-untyped-def]
    """A story sitting at SELECTED, ready for the script stage."""
    story = make_story(title="AITA for telling my sister the truth?")
    app.stories.upsert(story)
    for status, stage in (
        (StoryStatus.NORMALIZED, PipelineStage.NORMALIZE),
        (StoryStatus.DEDUPLICATED, PipelineStage.DEDUPLICATE),
        (StoryStatus.RANKED, PipelineStage.RANK),
        (StoryStatus.SELECTED, PipelineStage.SELECT),
    ):
        app.stories.transition(story.id, status, stage=stage)
    return app.stories.require(story.id)


class TestScriptStage:
    def test_a_selected_story_becomes_a_script(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        report = ProductionRunner(app).script()
        assert report.completed == 1
        assert report.artifacts >= 1
        assert app.stories.require(selected_story.id).status is StoryStatus.SCRIPT_READY

    def test_the_script_is_persisted_with_its_lines(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        ProductionRunner(app).script()
        scripts = app.scripts.for_story(selected_story.id, selected_story.provenance)
        assert scripts
        script = scripts[0]
        assert script.lines
        assert script.provenance.canonical_url == selected_story.canonical_url

    def test_re_running_updates_rather_than_duplicates(
        self, app: Application, selected_story
    ) -> None:  # type: ignore[no-untyped-def]
        ProductionRunner(app).script()
        before = app.scripts.count()
        ProductionRunner(app).script(force=True)
        assert app.scripts.count() == before

    def test_a_series_is_persisted_with_its_parts(self, app: Application, make_story) -> None:  # type: ignore[no-untyped-def]
        # Sized to need several parts but stay inside `max_parts`. Speech
        # shaping expands the numerals, so this runs longer than it reads.
        long_body = " ".join(f"Sentence number {n} of a very long story." for n in range(1, 120))
        story = make_story(body=long_body, source_id="t3_long001")
        app.stories.upsert(story)
        for status, stage in (
            (StoryStatus.NORMALIZED, PipelineStage.NORMALIZE),
            (StoryStatus.DEDUPLICATED, PipelineStage.DEDUPLICATE),
            (StoryStatus.RANKED, PipelineStage.RANK),
            (StoryStatus.SELECTED, PipelineStage.SELECT),
        ):
            app.stories.transition(story.id, status, stage=stage)

        ProductionRunner(app).script()
        scripts = app.scripts.for_story(story.id, story.provenance)
        assert len(scripts) > 1
        assert [s.part_number for s in scripts] == list(range(1, len(scripts) + 1))
        assert {s.total_parts for s in scripts} == {len(scripts)}
        assert app.series.parts_for_story(story.id, story.provenance)

    def test_an_unscriptable_story_is_set_aside_not_failed(
        self, app: Application, make_story
    ) -> None:  # type: ignore[no-untyped-def]
        """Too long for the format is a verdict about the story, not an error."""
        enormous = " ".join(f"Sentence {n} of an enormous story." for n in range(1, 4000))
        story = make_story(body=enormous, source_id="t3_huge001")
        app.stories.upsert(story)
        for status, stage in (
            (StoryStatus.NORMALIZED, PipelineStage.NORMALIZE),
            (StoryStatus.DEDUPLICATED, PipelineStage.DEDUPLICATE),
            (StoryStatus.RANKED, PipelineStage.RANK),
            (StoryStatus.SELECTED, PipelineStage.SELECT),
        ):
            app.stories.transition(story.id, status, stage=stage)

        report = ProductionRunner(app).script()
        assert report.rejected == 1
        assert report.failures == 0
        assert app.stories.require(story.id).status is StoryStatus.REJECTED


class TestScriptBuilder:
    def test_a_story_with_no_text_is_refused(self, config: AppConfig, make_story) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ScriptError):
            ScriptBuilder(config=config).build(make_story(body="..."))

    def test_a_too_short_story_is_refused(self, config: AppConfig, make_story) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ScriptError, match="too short"):
            ScriptBuilder(config=config).build(make_story(body="Three words only."))

    def test_a_too_long_story_is_refused(self, config: AppConfig, make_story) -> None:  # type: ignore[no-untyped-def]
        enormous = " ".join(f"Sentence {n} of an enormous story." for n in range(1, 4000))
        with pytest.raises(StoryTooLongError):
            ScriptBuilder(config=config).build(make_story(body=enormous))

    def test_speech_text_differs_from_display_text(self, config: AppConfig, make_story) -> None:  # type: ignore[no-untyped-def]
        """Captions show "$40"; the narrator says "forty dollars"."""
        story = make_story(body="He owed me $40. " + ("Filler sentence here. " * 40))
        script = ScriptBuilder(config=config).build(story).scripts[0]
        body = next(line for line in script.lines if "$40" in line.text)
        assert "forty dollars" in body.speech_text

    def test_building_is_deterministic(self, config: AppConfig, make_story) -> None:  # type: ignore[no-untyped-def]
        story = make_story()
        builder = ScriptBuilder(config=config)
        first, second = builder.build(story), builder.build(story)
        assert [s.id for s in first.scripts] == [s.id for s in second.scripts]
        assert first.scripts[0].speech_text == second.scripts[0].speech_text


class TestNarrationStage:
    def test_a_scripted_story_gets_audio(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        runner = ProductionRunner(app)
        runner.script()
        report = runner.narrate(provider="mock")
        assert report.completed == 1
        assert app.stories.require(selected_story.id).status is StoryStatus.AUDIO_READY

    def test_audio_is_persisted_with_word_timings(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        audio = app.audio.for_script(script.id, selected_story.provenance)
        assert audio is not None
        assert audio.exists()
        assert audio.duration_seconds > 0
        assert audio.has_alignment

    def test_word_timings_end_where_the_track_ends(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        """The measured clip length is the contract captions depend on."""
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        audio = app.audio.for_script(script.id, selected_story.provenance)
        assert audio is not None
        assert audio.word_timings[-1].end_seconds <= audio.duration_seconds + 0.01

    def test_re_narrating_reuses_the_cache(self, config: AppConfig, tmp_path, make_story) -> None:  # type: ignore[no-untyped-def]
        script = ScriptBuilder(config=config).build(make_story()).scripts[0]
        synth = NarrationSynthesizer(
            config=config.tts,
            provider=build_tts_provider(config, name="mock"),
            cache_dir=tmp_path / "audio",
        )
        first = synth.synthesize(script)
        second = synth.synthesize(script)
        assert first.cached is False
        assert second.cached is True
        assert second.lines_cached == second.lines_total

    def test_an_unavailable_provider_reports_rather_than_crashing(
        self, app: Application, selected_story
    ) -> None:  # type: ignore[no-untyped-def]
        """A missing model is a configuration state, not a pipeline failure."""
        runner = ProductionRunner(app)
        runner.script()
        report = runner.narrate(provider="kokoro")
        if report.notes:
            assert report.completed == 0
            assert report.failures == 0
            assert app.stories.require(selected_story.id).status is StoryStatus.SCRIPT_READY


@needs_ffmpeg
class TestRenderAndValidate:
    def test_the_full_chain_produces_a_validated_video(
        self, app: Application, selected_story
    ) -> None:  # type: ignore[no-untyped-def]
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        assert runner.render().completed == 1
        assert app.stories.require(selected_story.id).status is StoryStatus.VIDEO_READY

        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        video = app.videos.for_script(script.id, selected_story.provenance)
        assert video is not None
        assert video.exists()
        assert (video.width, video.height) == (app.config.render.width, app.config.render.height)
        # No clips in the (isolated, empty) library, so `auto` generates one.
        assert video.background_source == "procedural"

    def test_a_library_clip_is_used_when_one_exists(
        self, app: Application, selected_story, tmp_path: Path
    ) -> None:  # type: ignore[no-untyped-def]
        """`auto` switches to real footage the moment it appears.

        This is what makes "no gameplay footage yet" a working state rather than
        a broken one: nothing is configured, the clip is simply there.
        """
        library = app.config.background_library_dir
        library.mkdir(parents=True, exist_ok=True)
        clip = library / "clip.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=640x360:rate=15:duration=30",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "40",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
            timeout=120,
        )

        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        assert runner.render().completed == 1

        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        video = app.videos.for_script(script.id, selected_story.provenance)
        assert video is not None
        assert video.background_source == "clip.mp4"
        # A 30s clip behind a longer narration has to loop to fill the frame.
        assert video.metadata["background_looped"] is True
        assert (video.width, video.height) == (1080, 1920)

    def test_silent_audio_is_refused_by_validation(self, app: Application, selected_story) -> None:  # type: ignore[no-untyped-def]
        """The mock provider writes silence, so this must not pass.

        This is the gate doing its job: the exact failure it exists to catch is
        a video that looks fine and has no narration.
        """
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        runner.render()
        report = runner.validate()

        assert report.rejected == 1
        assert app.stories.require(selected_story.id).status is not StoryStatus.VALIDATED

        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        video = app.videos.for_script(script.id, selected_story.provenance)
        assert video is not None
        verdict = app.validations.latest(video.id)
        assert verdict is not None
        assert verdict.passed is False
        assert any("not_silent" in failure for failure in verdict.failures)

    def test_every_check_is_recorded_including_the_passing_ones(
        self, app: Application, selected_story
    ) -> None:  # type: ignore[no-untyped-def]
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        runner.render()
        runner.validate()

        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        video = app.videos.for_script(script.id, selected_story.provenance)
        assert video is not None
        verdict = app.validations.latest(video.id)
        assert verdict is not None
        assert verdict.checks["duration"]["passed"] is True
        assert "value" in verdict.checks["duration"]

    def test_captions_are_generated_from_the_measured_timings(
        self, app: Application, selected_story
    ) -> None:  # type: ignore[no-untyped-def]
        runner = ProductionRunner(app)
        runner.script()
        runner.narrate(provider="mock")
        script = app.scripts.for_story(selected_story.id, selected_story.provenance)[0]
        audio = app.audio.for_script(script.id, selected_story.provenance)
        assert audio is not None
        cues = group_into_cues(audio.word_timings, app.config.captions)
        assert cues
        assert cues[-1].end_seconds <= audio.duration_seconds + 0.01
