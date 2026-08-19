"""The publish stage.

Publishing is the one irreversible thing this pipeline does, so the ordering is
defensive by design:

1. Refuse a video that has not passed validation. Everything upstream is
   recoverable; a bad public upload is not.
2. Check the local daily cap before contacting the platform, so a runaway loop
   is stopped here rather than by a platform suspending the account.
3. Write the attempt row *before* transmitting. A process that dies mid-upload
   leaves a record naming exactly which video and which platform were in flight.
4. Let `UNIQUE (video_id, target)` make a retry an update rather than a second
   upload.

`dry_run` defaults to on in configuration and has to be turned off deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pulpmill.config.models import AppConfig, PublishTargetConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.domain.errors import (
    InvalidStateTransitionError,
    PublishError,
    PublisherUnavailableError,
)
from pulpmill.domain.media import VideoArtifact
from pulpmill.domain.publishing import (
    PublisherHealth,
    PublishRequest,
    PublishResult,
    PublishState,
)
from pulpmill.domain.script import NarrationScript
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock
from pulpmill.infrastructure.logging import get_logger
from pulpmill.persistence.repositories.media import ValidationRepository
from pulpmill.persistence.repositories.publications import PublicationRepository
from pulpmill.persistence.repositories.scripts import ScriptRepository
from pulpmill.persistence.repositories.stories import StoryRepository
from pulpmill.publishing.base import Publisher, PublisherContext, create_publisher
from pulpmill.publishing.metadata import build_metadata

_log = get_logger("publishing.service")


@dataclass(slots=True)
class PublishReport:
    dry_run: bool
    attempted: int = 0
    published: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[tuple[str, PublishResult]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "attempted": self.attempted,
            "published": self.published,
            "skipped": self.skipped,
            "failed": self.failed,
        }


@dataclass(slots=True)
class RelinkReport:
    dry_run: bool
    examined: int = 0
    updated: int = 0
    would_update: int = 0
    unchanged: int = 0
    #: Platforms whose captions cannot be edited after publishing.
    unsupported: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "examined": self.examined,
            "updated": self.updated,
            "would_update": self.would_update,
            "unchanged": self.unchanged,
            "unsupported": self.unsupported,
            "failed": self.failed,
        }


class PublishingService:
    """Publishes validated videos to the enabled targets."""

    def __init__(
        self,
        *,
        config: AppConfig,
        secrets: SecretStore,
        stories: StoryRepository,
        publications: PublicationRepository,
        validations: ValidationRepository,
        scripts: ScriptRepository | None = None,
        clock: Clock = SYSTEM_CLOCK,
        transport: object | None = None,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._stories = stories
        self._scripts = scripts
        self._publications = publications
        self._validations = validations
        self._clock = clock
        #: Test seam, mirroring the ingestion adapters. Never set in production.
        self._transport = transport

    def targets(self, names: list[str] | None = None) -> dict[str, PublishTargetConfig]:
        """Configured targets, optionally filtered. A named target runs even when
        it is disabled, so `--target x` is a usable way to rehearse one."""
        configured = self._config.publishing.targets
        if names is None:
            return dict(self._config.publishing.enabled_targets())
        selected: dict[str, PublishTargetConfig] = {}
        for name in names:
            target = configured.get(name)
            if target is None:
                raise PublishError(
                    "no such publishing target",
                    target=name,
                    available=", ".join(sorted(configured)) or "none",
                )
            selected[name] = target
        return selected

    def health(self) -> dict[str, PublisherHealth]:
        """Per-target readiness, for `pulpmill targets`."""
        report: dict[str, PublisherHealth] = {}
        for name, target in self._config.publishing.targets.items():
            publisher = create_publisher(
                PublisherContext(
                    name=name,
                    config=self._config,
                    target=target,
                    secrets=self._secrets,
                    clock=self._clock,
                    transport=self._transport,
                )
            )
            try:
                report[name] = publisher.health()
            finally:
                publisher.close()
        return report

    def publish(
        self,
        video: VideoArtifact,
        script: NarrationScript,
        *,
        target_names: list[str] | None = None,
        dry_run: bool | None = None,
        job_id: str | None = None,
    ) -> PublishReport:
        """Publish one video to every selected target."""
        effective_dry_run = self._config.publishing.dry_run if dry_run is None else dry_run
        report = PublishReport(dry_run=effective_dry_run)

        verdict = self._validations.latest(video.id)
        if verdict is None or not verdict.passed:
            reason = "never validated" if verdict is None else "failed validation"
            report.notes.append(f"refusing to publish: video {reason}")
            report.skipped += 1
            _log.warning(
                "publish_blocked_by_validation",
                video_id=video.id,
                story_id=video.story_id,
                reason=reason,
            )
            return report

        if not video.exists():
            raise PublishError("rendered file is missing", path=str(video.path))

        selected = self.targets(target_names)
        if not selected:
            report.notes.append("no publishing targets are enabled")
            return report

        succeeded: list[str] = []
        for name, target in selected.items():
            result = self._publish_one(
                name,
                target,
                video=video,
                script=script,
                dry_run=effective_dry_run,
            )
            report.results.append((name, result))
            report.attempted += 1
            if result.state is PublishState.PUBLISHED:
                report.published += 1
                succeeded.append(name)
            elif result.state is PublishState.SKIPPED:
                report.skipped += 1
            else:
                report.failed += 1

        if succeeded and not effective_dry_run:
            self._mark_published(video.story_id, job_id=job_id, targets=succeeded)

        return report

    def _publish_one(
        self,
        name: str,
        target: PublishTargetConfig,
        *,
        video: VideoArtifact,
        script: NarrationScript,
        dry_run: bool,
    ) -> PublishResult:
        existing = self._publications.get(video.id, name)
        if existing is not None and existing.state is PublishState.PUBLISHED:
            return PublishResult(
                target=name,
                state=PublishState.SKIPPED,
                remote_id=existing.remote_id,
                remote_url=existing.remote_url,
                detail="already published to this target",
            )

        if not dry_run:
            used = self._publications.published_today(name)
            if used >= target.daily_limit:
                _log.warning(
                    "publish_quota_reached", target=name, used=used, limit=target.daily_limit
                )
                return PublishResult(
                    target=name,
                    state=PublishState.SKIPPED,
                    detail=f"local daily limit reached ({used}/{target.daily_limit})",
                )

        # Whatever is already live for this story on this target, so a viewer
        # arriving at part three can walk back to part one.
        siblings = self._publications.published_siblings(
            video.story_id, name, exclude_video_id=video.id
        )
        metadata = build_metadata(script, config=self._config, target=target, siblings=siblings)
        request = PublishRequest(
            video_path=video.path,
            metadata=metadata,
            story_id=video.story_id,
            script_id=video.script_id,
            video_id=video.id,
            dry_run=dry_run,
        )

        publisher = create_publisher(
            PublisherContext(
                name=name,
                config=self._config,
                target=target,
                secrets=self._secrets,
                clock=self._clock,
                transport=self._transport,
            )
        )
        publication_id = self._publications.begin(
            video_id=video.id,
            script_id=video.script_id,
            story_id=video.story_id,
            target=name,
            adapter=target.adapter,
            privacy=metadata.privacy,
            dry_run=dry_run,
            request=request.to_record(),
        )

        try:
            result = publisher.publish(request)
        except PublisherUnavailableError as exc:
            self._publications.complete(publication_id, state=PublishState.SKIPPED, error=str(exc))
            _log.warning("publish_target_unavailable", target=name, error=str(exc))
            return PublishResult(target=name, state=PublishState.SKIPPED, detail=str(exc))
        except PublishError as exc:
            self._publications.complete(publication_id, state=PublishState.FAILED, error=str(exc))
            _log.error(
                "publish_failed",
                target=name,
                story_id=video.story_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return PublishResult(target=name, state=PublishState.FAILED, detail=str(exc))
        finally:
            publisher.close()

        self._publications.complete(
            publication_id,
            state=result.state,
            remote_id=result.remote_id,
            remote_url=result.remote_url,
            response=dict(result.response),
        )
        return result

    # --- relinking ------------------------------------------------------------

    def relink(
        self,
        *,
        target_names: list[str] | None = None,
        story_id: str | None = None,
        dry_run: bool = True,
    ) -> RelinkReport:
        """Backfill series cross-links into already-published descriptions.

        Necessary because publishing is ordered: part one goes up before part
        two exists, so its description cannot link forward at the time. Once the
        whole series is live, every part's index can be completed.

        Only where the platform allows it. Instagram and TikTok publish captions
        immutably, so their earlier parts stay pointing backwards -- reported,
        not silently skipped.
        """
        if self._scripts is None:  # pragma: no cover - wiring error
            raise PublishError("relink needs the script repository")

        report = RelinkReport(dry_run=dry_run)
        for name, target in self.targets(target_names).items():
            publisher = create_publisher(
                PublisherContext(
                    name=name,
                    config=self._config,
                    target=target,
                    secrets=self._secrets,
                    clock=self._clock,
                    transport=self._transport,
                )
            )
            try:
                candidates = (
                    [story_id] if story_id else self._publications.stories_with_multiple_parts(name)
                )
                for candidate in candidates:
                    self._relink_story(candidate, name, target, publisher, report, dry_run)
            finally:
                publisher.close()
        return report

    def _relink_story(
        self,
        story_id: str,
        target_name: str,
        target: PublishTargetConfig,
        publisher: Publisher,
        report: RelinkReport,
        dry_run: bool,
    ) -> None:
        assert self._scripts is not None  # narrowed by the caller
        records = self._publications.published_parts_for_story(story_id, target_name)
        if len(records) < 2:
            return

        story = self._stories.get(story_id)
        if story is None:  # pragma: no cover - a deleted story with live videos
            report.notes.append(f"{story_id[:8]}: story is gone; leaving its videos alone")
            return

        siblings = self._publications.published_siblings(story_id, target_name)

        for record in records:
            script = self._scripts.get(record.script_id, story.provenance)
            if script is None or record.remote_id is None:
                continue
            metadata = build_metadata(script, config=self._config, target=target, siblings=siblings)
            report.examined += 1

            if metadata.description == record.request.get("description"):
                report.unchanged += 1
                continue
            if dry_run:
                report.would_update += 1
                continue

            try:
                updated = publisher.update_metadata(record.remote_id, metadata)
            except PublishError as exc:
                report.failed += 1
                _log.error(
                    "relink_failed",
                    target=target_name,
                    story_id=story_id,
                    remote_id=record.remote_id,
                    error=str(exc),
                )
                continue

            if not updated:
                report.unsupported += 1
                continue

            report.updated += 1
            self._publications.complete(
                record.id,
                state=PublishState.PUBLISHED,
                remote_id=record.remote_id,
                remote_url=record.remote_url,
                response=dict(record.response),
            )
            self._publications.replace_request(
                record.id, {**dict(record.request), "description": metadata.description}
            )

    def _mark_published(self, story_id: str, *, job_id: str | None, targets: list[str]) -> None:
        try:
            self._stories.transition(
                story_id,
                StoryStatus.PUBLISHED,
                stage=PipelineStage.PUBLISH,
                job_id=job_id,
                reason=f"published to {', '.join(targets)}",
            )
        except InvalidStateTransitionError as exc:
            # A multi-part story reaches PUBLISHED on its first part. Later
            # parts finding it already terminal is expected, not a failure.
            _log.info("publish_transition_skipped", story_id=story_id, reason=str(exc))
