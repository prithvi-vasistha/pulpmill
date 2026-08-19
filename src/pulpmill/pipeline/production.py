"""The production stages: SELECTED becomes a validated file on disk.

    script -> narrate -> render -> validate

Each stage advances whole *stories*, not individual parts. A three-part story
has three scripts, three audio tracks and three videos, but one status -- so a
story only reaches `AUDIO_READY` when every one of its parts has audio. Advancing
per part would let a story be half-rendered and still look ready to publish.

Restartability follows the ingestion stages: every artifact is committed as it
is produced, and every stage is idempotent. Killing the process mid-render loses
at most the video in flight, and re-running picks up from the database.

`PENDING` states are entered before the expensive work and left after it, so an
interrupted run is visible as `AUDIO_PENDING` rather than as a story that
silently stopped moving.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pulpmill.captions.cues import group_into_cues
from pulpmill.domain.enums import JobStatus, PipelineStage, StoryStatus
from pulpmill.domain.errors import (
    AssetError,
    InvalidStateTransitionError,
    PersistenceError,
    PulpmillError,
    RenderError,
    ScriptError,
    StoryTooLongError,
    SynthesisError,
)
from pulpmill.domain.media import AudioArtifact, VideoArtifact
from pulpmill.domain.script import NarrationScript
from pulpmill.domain.story import Story
from pulpmill.infrastructure.logging import get_logger
from pulpmill.persistence.repositories.jobs import FailureRecord
from pulpmill.pipeline.context import Application
from pulpmill.rendering.backgrounds import build_background_provider
from pulpmill.rendering.compositor import VideoCompositor
from pulpmill.scripting.service import ScriptBuilder, build_script_provider
from pulpmill.tts.service import NarrationSynthesizer, build_tts_provider
from pulpmill.validation.checks import validate_file


@dataclass(slots=True)
class StageReport:
    """One production stage's outcome."""

    stage: str
    job_id: str
    considered: int = 0
    completed: int = 0
    skipped: int = 0
    rejected: int = 0
    failures: int = 0
    artifacts: int = 0
    duration_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "considered": self.considered,
            "completed": self.completed,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "failures": self.failures,
            "artifacts": self.artifacts,
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass(slots=True)
class ProduceReport:
    stages: list[StageReport] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {report.stage: report.as_dict() for report in self.stages}


class ProductionRunner:
    """Runs the script, narration, render and validation stages."""

    def __init__(self, app: Application) -> None:
        self._app = app
        self._log = get_logger("pipeline.production")

    # --- script --------------------------------------------------------------

    def script(self, *, limit: int | None = None, force: bool = False) -> StageReport:
        """Turn selected stories into narration scripts."""
        app = self._app
        builder = ScriptBuilder(
            config=app.config,
            provider=build_script_provider(app.config, app.secrets),
            clock=app.clock,
        )
        report = self._start("script", {"force": force, "limit": limit})
        started = time.monotonic()

        statuses = (
            (StoryStatus.SELECTED, StoryStatus.SCRIPT_PENDING, StoryStatus.SCRIPT_READY)
            if force
            else (StoryStatus.SELECTED, StoryStatus.SCRIPT_PENDING)
        )

        try:
            for story in self._take(app.stories.iter_by_status(statuses), limit, report):
                self._script_one(story, builder, report)
        except BaseException as exc:
            self._fail(report, exc)
            raise

        report.duration_seconds = time.monotonic() - started
        self._finish(report)
        return report

    def _script_one(self, story: Story, builder: ScriptBuilder, report: StageReport) -> None:
        app = self._app
        try:
            self._advance(story.id, StoryStatus.SCRIPT_PENDING, PipelineStage.SCRIPT, report.job_id)
            result = builder.build(story)
        except StoryTooLongError as exc:
            # Not a failure: this story cannot be told in the configured format.
            # It is set aside, with the reason on the state event.
            report.rejected += 1
            self._advance(
                story.id,
                StoryStatus.REJECTED,
                PipelineStage.SCRIPT,
                report.job_id,
                reason=str(exc),
            )
            self._log.info("script_rejected_too_long", story_id=story.id, reason=str(exc))
            return
        except ScriptError as exc:
            self._record_failure(report, story, PipelineStage.SCRIPT, "build_script", exc)
            return

        app.series.save(series_id=result.series_id, story_id=story.id, parts=result.parts)
        for script in result.scripts:
            app.scripts.save(
                script,
                requested_provider=result.provider,
                fallback_reason=result.fallback_reason,
                notes=result.notes,
                words_per_minute=app.config.script.words_per_minute,
            )
        report.artifacts += len(result.scripts)
        report.completed += 1
        self._advance(story.id, StoryStatus.SCRIPT_READY, PipelineStage.SCRIPT, report.job_id)

    # --- narrate -------------------------------------------------------------

    def narrate(
        self, *, limit: int | None = None, force: bool = False, provider: str | None = None
    ) -> StageReport:
        """Synthesise narration for every part of each scripted story."""
        app = self._app
        synthesizer = NarrationSynthesizer(
            config=app.config.tts,
            provider=build_tts_provider(app.config, name=provider),
            cache_dir=app.config.audio_cache_dir,
        )
        report = self._start("narrate", {"provider": synthesizer.provider_name, "force": force})
        started = time.monotonic()

        ready, detail = synthesizer.available()
        if not ready:
            report.notes.append(detail)
            self._log.warning("tts_unavailable", provider=synthesizer.provider_name, reason=detail)
            report.duration_seconds = time.monotonic() - started
            self._finish(report)
            return report

        statuses = (
            (StoryStatus.SCRIPT_READY, StoryStatus.AUDIO_PENDING, StoryStatus.AUDIO_READY)
            if force
            else (StoryStatus.SCRIPT_READY, StoryStatus.AUDIO_PENDING)
        )

        try:
            for story in self._take(app.stories.iter_by_status(statuses), limit, report):
                self._narrate_one(story, synthesizer, report, force=force)
        except BaseException as exc:
            self._fail(report, exc)
            raise

        report.duration_seconds = time.monotonic() - started
        self._finish(report)
        return report

    def _narrate_one(
        self,
        story: Story,
        synthesizer: NarrationSynthesizer,
        report: StageReport,
        *,
        force: bool,
    ) -> None:
        app = self._app
        scripts = app.scripts.for_story(story.id, story.provenance)
        if not scripts:
            report.skipped += 1
            report.notes.append(f"{story.id}: no scripts to narrate")
            return

        self._advance(story.id, StoryStatus.AUDIO_PENDING, PipelineStage.TTS, report.job_id)
        for script in scripts:
            try:
                outcome = synthesizer.synthesize(script, force=force)
            except SynthesisError as exc:
                self._record_failure(report, story, PipelineStage.TTS, "synthesize", exc)
                return
            app.audio.save(outcome.artifact, cache_key=outcome.cache_key)
            report.artifacts += 1

        report.completed += 1
        self._advance(story.id, StoryStatus.AUDIO_READY, PipelineStage.TTS, report.job_id)

    # --- render --------------------------------------------------------------

    def render(self, *, limit: int | None = None, force: bool = False) -> StageReport:
        """Compose video for every part of each narrated story."""
        app = self._app
        compositor = self._compositor()
        report = self._start("render", {"force": force, "encoder": app.config.render.encoder})
        started = time.monotonic()

        statuses = (
            (StoryStatus.AUDIO_READY, StoryStatus.VIDEO_PENDING, StoryStatus.VIDEO_READY)
            if force
            else (StoryStatus.AUDIO_READY, StoryStatus.VIDEO_PENDING)
        )

        try:
            for story in self._take(app.stories.iter_by_status(statuses), limit, report):
                self._render_one(story, compositor, report, force=force)
        except BaseException as exc:
            self._fail(report, exc)
            raise

        report.duration_seconds = time.monotonic() - started
        self._finish(report)
        return report

    def _render_one(
        self, story: Story, compositor: VideoCompositor, report: StageReport, *, force: bool
    ) -> None:
        app = self._app
        scripts = app.scripts.for_story(story.id, story.provenance)
        if not scripts:
            report.skipped += 1
            return

        self._advance(story.id, StoryStatus.VIDEO_PENDING, PipelineStage.RENDER, report.job_id)
        for script in scripts:
            audio = app.audio.for_script(script.id, story.provenance)
            if audio is None:
                report.skipped += 1
                report.notes.append(f"{script.id}: no audio")
                return
            cues = group_into_cues(audio.word_timings, app.config.captions)
            try:
                outcome = compositor.render(script, audio, cues, force=force)
            except (RenderError, AssetError) as exc:
                self._record_failure(report, story, PipelineStage.RENDER, "render", exc)
                return
            app.videos.save(outcome.artifact)
            report.artifacts += 1

        report.completed += 1
        self._advance(story.id, StoryStatus.VIDEO_READY, PipelineStage.RENDER, report.job_id)

    # --- validate ------------------------------------------------------------

    def validate(self, *, limit: int | None = None) -> StageReport:
        """Check rendered files against the publishability rules."""
        app = self._app
        report = self._start("validate", {})
        started = time.monotonic()

        try:
            for story in self._take(
                app.stories.iter_by_status((StoryStatus.VIDEO_READY, StoryStatus.VALIDATED)),
                limit,
                report,
            ):
                self._validate_one(story, report)
        except BaseException as exc:
            self._fail(report, exc)
            raise

        report.duration_seconds = time.monotonic() - started
        self._finish(report)
        return report

    def _validate_one(self, story: Story, report: StageReport) -> None:
        app = self._app
        scripts = app.scripts.for_story(story.id, story.provenance)
        all_passed = True

        for script in scripts:
            video = app.videos.for_script(script.id, story.provenance)
            if video is None:
                report.skipped += 1
                all_passed = False
                continue

            audio = app.audio.for_script(script.id, story.provenance)
            try:
                result = validate_file(
                    video.path,
                    config=app.config.validation,
                    render=app.config.render,
                    expected_duration=audio.duration_seconds if audio else None,
                    max_part_seconds=app.config.script.max_seconds,
                )
            except PulpmillError as exc:
                self._record_failure(report, story, PipelineStage.VALIDATE, "validate", exc)
                return

            app.validations.record(
                video_id=video.id,
                story_id=story.id,
                passed=result.passed,
                checks=result.as_dict(),
                failures=result.failures,
            )
            report.artifacts += 1
            if not result.passed:
                all_passed = False
                self._log.warning(
                    "validation_failed",
                    story_id=story.id,
                    script_id=script.id,
                    failures=list(result.failures),
                )

        if all_passed and scripts:
            report.completed += 1
            self._advance(story.id, StoryStatus.VALIDATED, PipelineStage.VALIDATE, report.job_id)
        else:
            report.rejected += 1

    # --- combined ------------------------------------------------------------

    def produce(self, *, limit: int | None = None, force: bool = False) -> ProduceReport:
        """Run every production stage in order."""
        return ProduceReport(
            stages=[
                self.script(limit=limit, force=force),
                self.narrate(limit=limit, force=force),
                self.render(limit=limit, force=force),
                self.validate(limit=limit),
            ]
        )

    # --- helpers -------------------------------------------------------------

    def _compositor(self) -> VideoCompositor:
        config = self._app.config
        return VideoCompositor(
            config=config.render,
            captions=config.captions,
            background=build_background_provider(
                config.render.background, library_dir=config.background_library_dir
            ),
            output_dir=config.video_output_dir,
            work_dir=config.data_dir / "render",
            production_fingerprint=config.production_fingerprint(),
        )

    def _take(self, stories: object, limit: int | None, report: StageReport) -> Sequence[Story]:
        """Materialise the work list before mutating any status.

        The stage queries are keyed on status and the stage then *changes* that
        status, so iterating lazily would have the cursor chase its own writes.
        """
        collected: list[Story] = []
        for story in stories:  # type: ignore[attr-defined]
            if limit is not None and len(collected) >= limit:
                break
            collected.append(story)
        report.considered = len(collected)
        return collected

    def _advance(
        self,
        story_id: str,
        status: StoryStatus,
        stage: PipelineStage,
        job_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        try:
            self._app.stories.transition(
                story_id, status, stage=stage, job_id=job_id, reason=reason
            )
        except InvalidStateTransitionError as exc:
            # Re-running a stage on a story already past it. Expected on
            # --force and on resume; recorded, never silently ignored.
            self._log.debug("transition_skipped", story_id=story_id, reason=str(exc))

    def _start(self, kind: str, params: dict[str, object]) -> StageReport:
        job_id = self._app.jobs.start(kind, params)
        return StageReport(stage=kind, job_id=job_id)

    def _finish(self, report: StageReport) -> None:
        self._app.jobs.finish(report.job_id, status=JobStatus.SUCCEEDED, stats=report.as_dict())
        self._log.info(f"{report.stage}_complete", **report.as_dict())

    def _fail(self, report: StageReport, exc: BaseException) -> None:
        self._app.jobs.finish(
            report.job_id,
            status=JobStatus.FAILED,
            stats=report.as_dict(),
            error=f"{type(exc).__name__}: {exc}",
        )

    def _record_failure(
        self,
        report: StageReport,
        story: Story,
        stage: PipelineStage,
        operation: str,
        exc: Exception,
    ) -> None:
        report.failures += 1
        self._app.failures.record(
            FailureRecord(
                stage=stage,
                operation=operation,
                error_type=type(exc).__name__,
                error_message=str(exc),
                source_platform=story.source_platform,
                story_id=story.id,
                context={"url": story.canonical_url},
            ),
            job_id=report.job_id,
        )
        self._log.error(
            f"{stage.value}_failed",
            story_id=story.id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        try:
            self._app.stories.transition(
                story.id,
                StoryStatus.FAILED,
                stage=stage,
                job_id=report.job_id,
                reason=f"{type(exc).__name__}: {exc}",
            )
        except (InvalidStateTransitionError, PersistenceError) as transition_exc:
            self._log.debug(
                "failure_transition_skipped", story_id=story.id, reason=str(transition_exc)
            )


def audio_for(app: Application, script: NarrationScript) -> AudioArtifact | None:
    provenance = script.provenance
    return app.audio.for_script(script.id, provenance)


def video_for(app: Application, script: NarrationScript) -> VideoArtifact | None:
    return app.videos.for_script(script.id, script.provenance)


def video_output_path(app: Application, script_id: str) -> Path:
    return app.config.video_output_dir / f"{script_id}.mp4"
