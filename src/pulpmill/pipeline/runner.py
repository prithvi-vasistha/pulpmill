"""The pipeline runner.

Implements tonight's vertical slice:

    fetch -> normalize -> deduplicate -> persist -> rank

Two properties matter more than throughput here:

**Streaming.** Stories are pulled one at a time from an adapter's generator and
fully processed before the next is requested. Peak memory is one story plus the
bounded novelty corpus, not one run's worth of posts.

**Restartability.** Every story is committed in its own transaction as it is
processed, and every state change is recorded. Killing the process mid-run loses
at most the story in flight; the next run picks up from the database, not from
memory.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

from pulpmill.config.models import SourceConfig
from pulpmill.deduplication.engine import DedupOutcome
from pulpmill.domain.enums import JobStatus, PipelineStage, StoryStatus
from pulpmill.domain.errors import (
    IngestionError,
    InvalidStateTransitionError,
    PersistenceError,
    SourceUnavailableError,
)
from pulpmill.domain.source import FetchRequest, SourceAdapter
from pulpmill.domain.story import Story
from pulpmill.infrastructure.clock import to_iso
from pulpmill.infrastructure.logging import get_logger
from pulpmill.ingestion.registry import AdapterContext, create_adapter
from pulpmill.persistence.repositories.jobs import FailureRecord
from pulpmill.pipeline.context import Application
from pulpmill.pipeline.reports import IngestReport, RankReport, RunReport, SourceReport

#: A RUNNING job older than this is assumed to belong to a process that died.
STALE_JOB_AGE = timedelta(hours=6)

#: Statuses eligible for ranking.
_RANKABLE = (StoryStatus.DEDUPLICATED, StoryStatus.RANKED)


class PipelineRunner:
    """Executes pipeline stages against an `Application`."""

    def __init__(self, app: Application) -> None:
        self._app = app
        self._log = get_logger("pipeline.runner")

    # --- ingest --------------------------------------------------------------

    def ingest(
        self,
        *,
        sources: Sequence[str] | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
    ) -> IngestReport:
        """Fetch, normalize, deduplicate and persist from the selected sources."""
        selected = self._select_sources(sources)
        config = self._app.config

        reclaimed = self._app.jobs.reclaim_stale(older_than=STALE_JOB_AGE)
        if reclaimed:
            self._log.warning("stale_jobs_reclaimed", count=reclaimed)

        job_id = self._app.jobs.start(
            "ingest",
            {
                "sources": list(selected),
                "limit": limit or config.ingestion.max_stories_per_source,
                "max_pages": max_pages or config.ingestion.max_pages_per_query,
            },
        )
        report = IngestReport(job_id=job_id)

        try:
            for name, source_config in selected.items():
                report.sources[name] = self._ingest_source(
                    name,
                    source_config,
                    job_id=job_id,
                    limit=limit or config.ingestion.max_stories_per_source,
                    max_pages=max_pages or config.ingestion.max_pages_per_query,
                )
                self._app.jobs.update_stats(job_id, report.as_dict())
        except BaseException as exc:
            # Includes KeyboardInterrupt: an operator stopping a 24/7 worker
            # should leave an accurate job record, not a RUNNING orphan.
            self._app.jobs.finish(
                job_id,
                status=JobStatus.FAILED,
                stats=report.as_dict(),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        self._app.jobs.finish(job_id, status=JobStatus.SUCCEEDED, stats=report.as_dict())
        self._log.info("ingest_complete", job_id=job_id, **_summary(report))
        return report

    def _select_sources(self, names: Sequence[str] | None) -> dict[str, SourceConfig]:
        config = self._app.config
        if names is None:
            return config.enabled_sources()

        selected: dict[str, SourceConfig] = {}
        for name in names:
            source_config = config.source(name)
            if source_config is None:
                raise IngestionError(
                    "no such source in configuration",
                    source=name,
                    available=", ".join(sorted(config.sources)),
                )
            # An explicitly named source runs even when `enabled: false`, so
            # `--source x` is a usable way to test a disabled adapter.
            selected[name] = source_config
        return selected

    def _ingest_source(
        self,
        name: str,
        source_config: SourceConfig,
        *,
        job_id: str,
        limit: int,
        max_pages: int,
    ) -> SourceReport:
        log = self._log.bind(source=name, job_id=job_id, stage=PipelineStage.FETCH.value)
        started = time.monotonic()
        report = SourceReport(platform=name)

        try:
            adapter = create_adapter(
                AdapterContext(
                    name=name,
                    config=self._app.config,
                    source_config=source_config,
                    secrets=self._app.secrets,
                    clock=self._app.clock,
                    rng=self._app.rng,
                )
            )
        except IngestionError as exc:
            report.available = False
            report.detail = str(exc)
            report.failures += 1
            self._record_failure(
                job_id,
                stage=PipelineStage.FETCH,
                operation="create_adapter",
                exc=exc,
                source_platform=name,
            )
            log.error("adapter_unavailable", error=str(exc))
            return report

        try:
            health = adapter.health()
            report.available = health.available
            report.detail = health.detail
            report.remediation = health.remediation

            if not health.available:
                # Not a failure: a source with no credentials is a
                # configuration state, and the run continues without it.
                log.warning(
                    "source_skipped",
                    reason=health.detail,
                    remediation=health.remediation,
                )
                return report

            request = FetchRequest(
                queries=tuple(source_config.queries),
                limit=limit,
                max_pages=max_pages,
            )
            self._drain(adapter, request, report=report, job_id=job_id, name=name)
        except SourceUnavailableError as exc:
            report.available = False
            report.detail = str(exc)
            log.warning("source_unavailable", error=str(exc))
        except IngestionError as exc:
            report.failures += 1
            self._record_failure(
                job_id,
                stage=PipelineStage.FETCH,
                operation="fetch",
                exc=exc,
                source_platform=name,
            )
            log.error("source_fetch_failed", error=str(exc), error_type=type(exc).__name__)
        finally:
            adapter.close()
            report.duration_seconds = time.monotonic() - started

        log.info("source_complete", **report.as_dict())
        return report

    def _drain(
        self,
        adapter: SourceAdapter,
        request: FetchRequest,
        *,
        report: SourceReport,
        job_id: str,
        name: str,
    ) -> None:
        """Consume an adapter's stream, processing one story at a time."""
        for raw in adapter.fetch(request):
            report.fetched += 1
            try:
                story = adapter.normalize(raw)
            except IngestionError as exc:
                report.failures += 1
                self._record_failure(
                    job_id,
                    stage=PipelineStage.NORMALIZE,
                    operation="normalize",
                    exc=exc,
                    source_platform=name,
                    context={"source_id": raw.source_id, "url": raw.canonical_url},
                )
                self._log.warning(
                    "normalize_failed",
                    source=name,
                    source_id=raw.source_id,
                    error=str(exc),
                )
                continue

            if story is None:
                report.filtered += 1
                continue

            try:
                self._persist(story, report=report, job_id=job_id, name=name)
            except (PersistenceError, InvalidStateTransitionError) as exc:
                report.failures += 1
                self._record_failure(
                    job_id,
                    stage=PipelineStage.PERSIST,
                    operation="persist",
                    exc=exc,
                    source_platform=name,
                    story_id=story.id,
                    context={"url": story.canonical_url},
                )
                self._log.error(
                    "persist_failed",
                    source=name,
                    story_id=story.id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    def _persist(self, story: Story, *, report: SourceReport, job_id: str, name: str) -> None:
        """Deduplicate then store one story, advancing it through its states."""
        verdict = self._app.deduplication.evaluate(story)

        if verdict.outcome is DedupOutcome.KNOWN:
            # Same post, already held. Refresh its counters and leave its
            # pipeline position untouched.
            result = self._app.stories.upsert(story, job_id=job_id)
            report.known += 1
            self._log.debug(
                "story_known",
                source=name,
                story_id=result.story.id,
                updated=result.updated,
                url=result.story.canonical_url,
            )
            return

        result = self._app.stories.upsert(story, job_id=job_id)
        stored = result.story

        self._app.stories.transition(
            stored.id,
            StoryStatus.NORMALIZED,
            stage=PipelineStage.NORMALIZE,
            job_id=job_id,
            reason="normalized from source payload",
        )

        if verdict.outcome is DedupOutcome.DUPLICATE and verdict.original_id and verdict.layer:
            self._app.stories.mark_duplicate(
                stored.id,
                duplicate_of_id=verdict.original_id,
                layer=verdict.layer,
                job_id=job_id,
            )
            report.duplicates += 1
            self._log.info(
                "story_duplicate",
                source=name,
                story_id=stored.id,
                original_id=verdict.original_id,
                layer=verdict.layer.value if verdict.layer else None,
                url=stored.canonical_url,
            )
            return

        self._app.stories.transition(
            stored.id,
            StoryStatus.DEDUPLICATED,
            stage=PipelineStage.DEDUPLICATE,
            job_id=job_id,
            reason="passed all deduplication layers",
        )
        report.new += 1
        self._log.info(
            "story_ingested",
            source=name,
            story_id=stored.id,
            title=stored.title[:80],
            words=stored.word_count,
            url=stored.canonical_url,
        )

    # --- rank ----------------------------------------------------------------

    def rank(
        self,
        *,
        reference_time: datetime | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> RankReport:
        """Score every rankable story.

        `reference_time` is captured once for the whole pass so that scores
        within a run are mutually comparable, and stored on each row so the
        ranking can be reproduced exactly.

        Stories already scored under the current version and configuration are
        skipped unless `force` is set -- that is what makes re-running `rank`
        cheap and idempotent.
        """
        app = self._app
        engine = app.ranking
        now = reference_time or app.clock.now()

        job_id = app.jobs.start(
            "rank",
            {
                "ranking_version": engine.version,
                "config_fingerprint": engine.config_fingerprint,
                "reference_time": to_iso(now),
                "force": force,
            },
        )
        report = RankReport(
            job_id=job_id,
            ranking_version=engine.version,
            config_fingerprint=engine.config_fingerprint,
            reference_time=to_iso(now),
        )

        novelty_config = app.config.ranking.novelty
        corpus = app.stories.novelty_corpus(
            limit=novelty_config.lookback_stories,
            compare_chars=novelty_config.compare_chars,
        )
        report.corpus_size = len(corpus)

        try:
            for story in app.stories.iter_by_status(_RANKABLE):
                if limit is not None and report.considered >= limit:
                    break
                report.considered += 1

                if not force:
                    existing = app.rankings.get(
                        story.id,
                        ranking_version=engine.version,
                        config_fingerprint=engine.config_fingerprint,
                    )
                    if existing is not None:
                        report.skipped += 1
                        continue

                try:
                    result = engine.rank(
                        story,
                        reference_time=now,
                        novelty_corpus=corpus,
                        ranked_at=app.clock.now(),
                    )
                    app.rankings.save(result)
                    if story.status is not StoryStatus.RANKED:
                        app.stories.transition(
                            story.id,
                            StoryStatus.RANKED,
                            stage=PipelineStage.RANK,
                            job_id=job_id,
                            reason=f"scored {result.final_score:.2f} "
                            f"({engine.version}/{engine.config_fingerprint[:8]})",
                        )
                    report.ranked += 1
                except (PersistenceError, InvalidStateTransitionError, ValueError) as exc:
                    report.failures += 1
                    self._record_failure(
                        job_id,
                        stage=PipelineStage.RANK,
                        operation="rank",
                        exc=exc,
                        source_platform=story.source_platform,
                        story_id=story.id,
                    )
                    self._log.error(
                        "rank_failed",
                        story_id=story.id,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
        except BaseException as exc:
            app.jobs.finish(
                job_id,
                status=JobStatus.FAILED,
                stats=report.as_dict(),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        app.jobs.finish(job_id, status=JobStatus.SUCCEEDED, stats=report.as_dict())
        self._log.info("rank_complete", **report.as_dict())
        return report

    # --- combined ------------------------------------------------------------

    def run(
        self,
        *,
        sources: Sequence[str] | None = None,
        limit: int | None = None,
        max_pages: int | None = None,
        force_rank: bool = False,
    ) -> RunReport:
        ingest = self.ingest(sources=sources, limit=limit, max_pages=max_pages)
        rank = self.rank(force=force_rank)
        return RunReport(ingest=ingest, rank=rank)

    # --- maintenance ---------------------------------------------------------

    def rededuplicate(self) -> dict[str, int]:
        """Re-evaluate stored stories against the current dedup configuration.

        Useful after enabling a layer or changing a threshold. Only ever *adds*
        duplicate links; it never resurrects a story that is already marked.
        """
        app = self._app
        job_id = app.jobs.start("rededuplicate", {"layers": list(app.deduplication.layers)})
        stats = {"examined": 0, "marked": 0, "failures": 0}

        try:
            for story in app.stories.iter_by_status((StoryStatus.DEDUPLICATED, StoryStatus.RANKED)):
                stats["examined"] += 1
                verdict = app.deduplication.evaluate(story)
                if not verdict.is_duplicate or not verdict.original_id or not verdict.layer:
                    continue
                try:
                    app.stories.mark_duplicate(
                        story.id,
                        duplicate_of_id=verdict.original_id,
                        layer=verdict.layer,
                        job_id=job_id,
                    )
                    stats["marked"] += 1
                except (PersistenceError, InvalidStateTransitionError, ValueError) as exc:
                    stats["failures"] += 1
                    self._record_failure(
                        job_id,
                        stage=PipelineStage.DEDUPLICATE,
                        operation="rededuplicate",
                        exc=exc,
                        story_id=story.id,
                    )
        except BaseException as exc:
            app.jobs.finish(
                job_id, status=JobStatus.FAILED, stats=stats, error=f"{type(exc).__name__}: {exc}"
            )
            raise

        app.jobs.finish(job_id, status=JobStatus.SUCCEEDED, stats=stats)
        return stats

    # --- helpers -------------------------------------------------------------

    def _record_failure(
        self,
        job_id: str,
        *,
        stage: PipelineStage,
        operation: str,
        exc: Exception,
        source_platform: str | None = None,
        story_id: str | None = None,
        context: Mapping[str, object] | None = None,
    ) -> None:
        """Persist a failure with every field needed to diagnose it later."""
        retry_count = getattr(exc, "attempts", None)
        self._app.failures.record(
            FailureRecord(
                stage=stage,
                operation=operation,
                error_type=type(exc).__name__,
                error_message=str(exc),
                source_platform=source_platform,
                story_id=story_id,
                retry_count=int(retry_count) if isinstance(retry_count, int) else 0,
                context=dict(context or {}),
            ),
            job_id=job_id,
        )


def _summary(report: IngestReport) -> dict[str, int]:
    return {
        "fetched": report.fetched,
        "new": report.new,
        "known": report.known,
        "duplicates": report.duplicates,
        "filtered": report.filtered,
        "failures": report.failures,
    }
