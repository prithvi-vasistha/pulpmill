"""Publishing, against a mocked transport.

No network. Every adapter's real request construction, auth handling and error
paths run against recorded responses, which is what makes the interesting
assertions possible: that a retry does not upload twice, that a dry run
transmits nothing, and that a video which failed validation is never sent.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.domain.media import VideoArtifact, build_video_id
from pulpmill.domain.publishing import PublishState
from pulpmill.domain.script import LineRole, NarrationScript, ScriptLine, build_script_id
from pulpmill.pipeline.context import Application
from pulpmill.publishing.service import PublishingService

pytestmark = pytest.mark.usefixtures("database")

YOUTUBE_SESSION_URL = "https://upload.example/session/1"


def enable_target(config: AppConfig, name: str, **overrides: object) -> AppConfig:
    """Return a config with one publishing target enabled."""
    target = config.publishing.targets[name].model_copy(update={"enabled": True, **overrides})
    return config.model_copy(
        update={
            "publishing": config.publishing.model_copy(
                update={"targets": {**config.publishing.targets, name: target}}
            )
        }
    )


def youtube_handler(*, calls: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    """A YouTube stand-in that issues a distinct id per upload.

    Distinct ids matter: two parts sharing one URL would make the series
    cross-linking assertions pass for the wrong reason.
    """
    uploaded = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        if request.url.host == "www.googleapis.com" and request.method == "POST":
            return httpx.Response(200, headers={"location": YOUTUBE_SESSION_URL}, json={})
        if request.method == "PUT" and request.url.host == "upload.example":
            return httpx.Response(
                200,
                json={
                    "id": f"vid-{next(uploaded)}",
                    "status": {"privacyStatus": "private"},
                },
            )
        # videos.update, which returns the resource it just wrote.
        return httpx.Response(200, json={"id": "vid-1", "snippet": {}})

    return handler


@pytest.fixture
def prepared(app: Application, make_story, tmp_path: Path):  # type: ignore[no-untyped-def]
    """A story walked to VALIDATED, with a script and a rendered file on disk."""
    story = make_story()
    app.stories.upsert(story)
    for status, stage in (
        (StoryStatus.NORMALIZED, PipelineStage.NORMALIZE),
        (StoryStatus.DEDUPLICATED, PipelineStage.DEDUPLICATE),
        (StoryStatus.RANKED, PipelineStage.RANK),
        (StoryStatus.SELECTED, PipelineStage.SELECT),
        (StoryStatus.SCRIPT_PENDING, PipelineStage.SCRIPT),
        (StoryStatus.SCRIPT_READY, PipelineStage.SCRIPT),
        (StoryStatus.AUDIO_PENDING, PipelineStage.TTS),
        (StoryStatus.AUDIO_READY, PipelineStage.TTS),
        (StoryStatus.VIDEO_PENDING, PipelineStage.RENDER),
        (StoryStatus.VIDEO_READY, PipelineStage.RENDER),
        (StoryStatus.VALIDATED, PipelineStage.VALIDATE),
    ):
        app.stories.transition(story.id, status, stage=stage)

    provenance = story.provenance
    script = NarrationScript(
        id=build_script_id(story.id, 1),
        story_id=story.id,
        part_number=1,
        total_parts=1,
        series_id=None,
        part_id=None,
        provenance=provenance,
        title="AITA for telling the truth?",
        lines=(
            ScriptLine(index=0, role=LineRole.HOOK, text="Hook.", speech_text="Hook."),
            ScriptLine(index=1, role=LineRole.BODY, text="Body.", speech_text="Body."),
        ),
        generator="deterministic",
        generator_version="test",
        config_fingerprint="fp",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={"community": "TrueOffMyChest"},
    )
    app.scripts.save(script, requested_provider="deterministic", words_per_minute=150.0)

    path = tmp_path / "video.mp4"
    path.write_bytes(b"\x00" * 2048)
    video = VideoArtifact(
        id=build_video_id(script.id),
        script_id=script.id,
        story_id=story.id,
        audio_id="audio-1",
        path=path,
        duration_seconds=45.0,
        width=1080,
        height=1920,
        fps=30.0,
        size_bytes=2048,
        encoder="libx264",
        background_source="procedural",
        production_fingerprint="fp",
        provenance=provenance,
    )
    # video_artifacts has a foreign key onto audio_artifacts, so that row has to
    # exist before the video can reference it.
    with app.database.transaction() as connection:
        connection.execute(
            "INSERT INTO audio_artifacts (id, script_id, story_id, path, duration_seconds, "
            "sample_rate, voice_id, provider, model_version, cache_key, created_at) "
            "VALUES ('audio-1', ?, ?, 'a.wav', 45.0, 24000, 'v', 'mock', 'm', 'k', "
            "'2026-01-01T00:00:00Z')",
            (script.id, story.id),
        )
    app.videos.save(video)
    return story, script, video


def add_part(
    app: Application, script: NarrationScript, *, part_number: int, path: Path
) -> tuple[NarrationScript, VideoArtifact]:
    """Add another publishable part to an existing story."""
    extra = replace(
        script,
        id=build_script_id(script.story_id, part_number),
        part_number=part_number,
        total_parts=part_number,
    )
    app.scripts.save(extra, requested_provider="deterministic", words_per_minute=150.0)

    # The real script builder produces every part together with a consistent
    # `total_parts`. Re-save the earlier ones so this helper matches: a part
    # whose total_parts still says 1 is not a series and never gets linked.
    for number in range(1, part_number):
        earlier = replace(
            script,
            id=build_script_id(script.story_id, number),
            part_number=number,
            total_parts=part_number,
        )
        app.scripts.save(earlier, requested_provider="deterministic", words_per_minute=150.0)

    audio_id = f"audio-{part_number}"
    with app.database.transaction() as connection:
        connection.execute(
            "INSERT INTO audio_artifacts (id, script_id, story_id, path, duration_seconds, "
            "sample_rate, voice_id, provider, model_version, cache_key, created_at) "
            "VALUES (?, ?, ?, 'a.wav', 45.0, 24000, 'v', 'mock', 'm', 'k', "
            "'2026-01-01T00:00:00Z')",
            (audio_id, extra.id, extra.story_id),
        )

    video = VideoArtifact(
        id=build_video_id(extra.id),
        script_id=extra.id,
        story_id=extra.story_id,
        audio_id=audio_id,
        path=path,
        duration_seconds=45.0,
        width=1080,
        height=1920,
        fps=30.0,
        size_bytes=2048,
        encoder="libx264",
        background_source="procedural",
        production_fingerprint="fp",
        provenance=extra.provenance,
    )
    app.videos.save(video)
    return extra, video


def service(
    app: Application, config: AppConfig, transport: object | None = None
) -> PublishingService:
    return PublishingService(
        config=config,
        secrets=SecretStore(
            environ={
                "PULPMILL_YOUTUBE_CLIENT_ID": "cid",
                "PULPMILL_YOUTUBE_CLIENT_SECRET": "csecret",
                "PULPMILL_YOUTUBE_REFRESH_TOKEN": "rtoken",
            }
        ),
        stories=app.stories,
        publications=app.publications,
        validations=app.validations,
        scripts=app.scripts,
        clock=app.clock,
        transport=transport,
    )


def pass_validation(app: Application, video: VideoArtifact) -> None:
    app.validations.record(
        video_id=video.id, story_id=video.story_id, passed=True, checks={}, failures=()
    )


class TestValidationGate:
    def test_an_unvalidated_video_is_never_published(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """Everything upstream is recoverable. A bad public upload is not."""
        _, script, video = prepared
        report = service(app, enable_target(config, "youtube")).publish(
            video, script, target_names=["youtube"], dry_run=False
        )
        assert report.published == 0
        assert any("never validated" in note for note in report.notes)

    def test_a_failed_video_is_never_published(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        app.validations.record(
            video_id=video.id,
            story_id=video.story_id,
            passed=False,
            checks={},
            failures=("not_silent: -91 dBFS",),
        )
        report = service(app, enable_target(config, "youtube")).publish(
            video, script, target_names=["youtube"], dry_run=False
        )
        assert report.published == 0
        assert any("failed validation" in note for note in report.notes)


class TestDryRun:
    def test_a_dry_run_transmits_nothing(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        pass_validation(app, video)
        calls: list[httpx.Request] = []
        report = service(
            app, enable_target(config, "youtube"), httpx.MockTransport(youtube_handler(calls=calls))
        ).publish(video, script, target_names=["youtube"], dry_run=True)

        assert calls == []
        assert report.published == 0
        assert report.skipped == 1

    def test_a_dry_run_still_builds_and_records_the_request(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        pass_validation(app, video)
        service(app, enable_target(config, "youtube")).publish(
            video, script, target_names=["youtube"], dry_run=True
        )
        record = app.publications.get(video.id, "youtube")
        assert record is not None
        assert record.dry_run is True
        assert record.request["title"]
        assert record.request["source_url"] == script.provenance.canonical_url

    def test_a_dry_run_works_without_credentials(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """Rehearsing is exactly what you want before approval is finished."""
        _, script, video = prepared
        pass_validation(app, video)
        report = PublishingService(
            config=enable_target(config, "youtube"),
            secrets=SecretStore(environ={}),
            stories=app.stories,
            publications=app.publications,
            validations=app.validations,
            clock=app.clock,
        ).publish(video, script, target_names=["youtube"], dry_run=True)

        assert report.skipped == 1
        result = report.results[0][1]
        assert "dry run" in result.detail


class TestLivePublishing:
    def test_a_video_is_uploaded_and_recorded(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        story, script, video = prepared
        pass_validation(app, video)
        calls: list[httpx.Request] = []
        report = service(
            app, enable_target(config, "youtube"), httpx.MockTransport(youtube_handler(calls=calls))
        ).publish(video, script, target_names=["youtube"], dry_run=False)

        assert report.published == 1
        result = report.results[0][1]
        assert result.state is PublishState.PUBLISHED
        assert result.remote_id == "vid-1"
        assert result.remote_url is not None
        assert app.stories.require(story.id).status.value == "PUBLISHED"

    def test_the_upload_carries_the_generated_metadata(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        pass_validation(app, video)
        calls: list[httpx.Request] = []
        service(
            app, enable_target(config, "youtube"), httpx.MockTransport(youtube_handler(calls=calls))
        ).publish(video, script, target_names=["youtube"], dry_run=False)

        metadata_request = next(
            call
            for call in calls
            if call.url.host == "www.googleapis.com" and call.method == "POST"
        )
        body = json.loads(metadata_request.content)
        assert body["snippet"]["title"]
        assert script.provenance.canonical_url in body["snippet"]["description"]
        assert body["status"]["privacyStatus"] == "private"

    def test_credentials_never_appear_in_the_stored_request(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        pass_validation(app, video)
        service(
            app, enable_target(config, "youtube"), httpx.MockTransport(youtube_handler(calls=[]))
        ).publish(video, script, target_names=["youtube"], dry_run=False)

        record = app.publications.get(video.id, "youtube")
        assert record is not None
        serialised = json.dumps(dict(record.request)) + json.dumps(dict(record.response))
        for secret in ("rtoken", "csecret", "at-1"):
            assert secret not in serialised

    def test_republishing_does_not_upload_a_second_copy(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """The UNIQUE constraint is what stands between a crash-loop and spam."""
        _, script, video = prepared
        pass_validation(app, video)
        enabled = enable_target(config, "youtube")
        calls: list[httpx.Request] = []
        transport = httpx.MockTransport(youtube_handler(calls=calls))

        service(app, enabled, transport).publish(
            video, script, target_names=["youtube"], dry_run=False
        )
        uploads_after_first = len(calls)
        report = service(app, enabled, transport).publish(
            video, script, target_names=["youtube"], dry_run=False
        )

        assert len(calls) == uploads_after_first
        assert report.results[0][1].state is PublishState.SKIPPED
        assert app.publications.count() == 1


class TestQuotas:
    def test_the_local_daily_cap_stops_uploads(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """A runaway loop is stopped here, not by a platform suspension."""
        _, script, video = prepared
        pass_validation(app, video)
        enabled = enable_target(config, "youtube", daily_limit=1)
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        service(app, enabled, transport).publish(
            video, script, target_names=["youtube"], dry_run=False
        )

        # A second publishable video needs its own script: one video per script
        # is enforced by a UNIQUE constraint, which is what makes retries safe.
        second_script, second_video = add_part(app, script, part_number=2, path=video.path)
        pass_validation(app, second_video)

        report = service(app, enabled, transport).publish(
            second_video, second_script, target_names=["youtube"], dry_run=False
        )
        result = report.results[0][1]
        assert result.state is PublishState.SKIPPED
        assert "daily limit" in result.detail

    def test_dry_runs_do_not_consume_quota(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        pass_validation(app, video)
        service(app, enable_target(config, "youtube")).publish(
            video, script, target_names=["youtube"], dry_run=True
        )
        assert app.publications.published_today("youtube") == 0


class TestTargetSelection:
    def test_an_unknown_target_is_an_error(self, app: Application, config: AppConfig) -> None:
        from pulpmill.domain.errors import PublishError

        with pytest.raises(PublishError, match="no such publishing target"):
            service(app, config).targets(["nope"])

    def test_a_named_target_runs_even_when_disabled(
        self, app: Application, config: AppConfig
    ) -> None:
        """So `--target x` is a usable way to rehearse one."""
        selected = service(app, config).targets(["youtube"])
        assert "youtube" in selected

    def test_only_enabled_targets_run_by_default(self, app: Application, config: AppConfig) -> None:
        assert service(app, config).targets() == {}


class TestHealth:
    def test_every_target_reports_a_remediation_when_blocked(
        self, app: Application, config: AppConfig
    ) -> None:
        """These platforms all gate on approval; retrying never clears it."""
        health = service(app, enable_target(config, "youtube")).health()
        assert set(health) == {"youtube", "instagram", "tiktok"}
        for status in health.values():
            if not status.available:
                assert status.detail


class TestSeriesCrossLinking:
    """A viewer arriving at part three must be able to walk back to part one."""

    def publish_part(self, app, config, transport, script, video):  # type: ignore[no-untyped-def]
        pass_validation(app, video)
        return service(app, enable_target(config, "youtube"), transport).publish(
            video, script, target_names=["youtube"], dry_run=False
        )

    def test_the_first_part_has_nothing_to_link(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_part(app, config, transport, script, video)

        record = app.publications.get(video.id, "youtube")
        assert record is not None
        assert "Watch the full story" not in record.request["description"]

    def test_a_later_part_links_the_earlier_ones(
        self, app: Application, config: AppConfig, prepared, tmp_path: Path
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_part(app, config, transport, script, video)

        second_script, second_video = add_part(app, script, part_number=2, path=video.path)
        self.publish_part(app, config, transport, second_script, second_video)

        record = app.publications.get(second_video.id, "youtube")
        assert record is not None
        assert "Watch the full story" in record.request["description"]
        assert "Part 1: https://www.youtube.com/shorts/vid-1" in record.request["description"]

    def test_a_part_never_links_itself(self, app: Application, config: AppConfig, prepared) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_part(app, config, transport, script, video)

        siblings = app.publications.published_siblings(
            video.story_id, "youtube", exclude_video_id=video.id
        )
        assert siblings == []

    def test_dry_runs_are_never_linked_to(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """A rehearsal has no URL, so there is nothing to point a viewer at."""
        _, script, video = prepared
        pass_validation(app, video)
        service(app, enable_target(config, "youtube")).publish(
            video, script, target_names=["youtube"], dry_run=True
        )
        assert app.publications.published_siblings(video.story_id, "youtube") == []


class TestRelink:
    """Publishing is ordered, so part one cannot link forward when it goes up."""

    def publish_both(self, app, config, transport, script, video):  # type: ignore[no-untyped-def]
        pass_validation(app, video)
        svc = service(app, enable_target(config, "youtube"), transport)
        svc.publish(video, script, target_names=["youtube"], dry_run=False)
        second_script, second_video = add_part(app, script, part_number=2, path=video.path)
        pass_validation(app, second_video)
        svc.publish(second_video, second_script, target_names=["youtube"], dry_run=False)
        return second_script, second_video

    def test_a_dry_run_reports_without_transmitting(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        calls: list[httpx.Request] = []
        transport = httpx.MockTransport(youtube_handler(calls=calls))
        self.publish_both(app, config, transport, script, video)

        before = len(calls)
        report = service(app, enable_target(config, "youtube"), transport).relink(
            target_names=["youtube"], dry_run=True
        )
        assert report.would_update >= 1
        assert report.updated == 0
        assert len(calls) == before

    def test_it_backfills_the_first_part(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_both(app, config, transport, script, video)

        report = service(app, enable_target(config, "youtube"), transport).relink(
            target_names=["youtube"], dry_run=False
        )
        assert report.updated >= 1

        record = app.publications.get(video.id, "youtube")
        assert record is not None
        assert "Watch the full story" in record.request["description"]
        assert "Part 2:" in record.request["description"]

    def test_relinking_twice_changes_nothing_the_second_time(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        """Otherwise a scheduled relink burns quota rewriting identical text."""
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_both(app, config, transport, script, video)

        svc = service(app, enable_target(config, "youtube"), transport)
        svc.relink(target_names=["youtube"], dry_run=False)
        second = svc.relink(target_names=["youtube"], dry_run=False)

        assert second.updated == 0
        assert second.unchanged == second.examined

    def test_a_single_part_story_is_left_alone(
        self, app: Application, config: AppConfig, prepared
    ) -> None:  # type: ignore[no-untyped-def]
        _, script, video = prepared
        transport = httpx.MockTransport(youtube_handler(calls=[]))
        self.publish_part = None  # unused here
        pass_validation(app, video)
        service(app, enable_target(config, "youtube"), transport).publish(
            video, script, target_names=["youtube"], dry_run=False
        )
        report = service(app, enable_target(config, "youtube"), transport).relink(
            target_names=["youtube"], dry_run=False
        )
        assert report.examined == 0
