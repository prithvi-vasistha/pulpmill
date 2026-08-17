"""The vertical slice: fetch -> normalize -> dedupe -> persist -> rank.

These tests drive `PipelineRunner` through a synthetic adapter registered at
runtime. That doubles as a test of the extension point itself: adding a source
really is "write an adapter, register it, add a config block" -- no core change.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from pulpmill.config.models import AppConfig, SourceConfig
from pulpmill.domain.enums import JobStatus, StoryStatus
from pulpmill.domain.errors import IngestionError, SourceResponseError, SourceUnavailableError
from pulpmill.domain.source import AdapterHealth, FetchRequest
from pulpmill.domain.story import Engagement, RawStory, Story
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY, build_story
from pulpmill.ingestion.registry import AdapterContext, register_adapter, registered_adapters
from pulpmill.persistence.database import Database
from pulpmill.persistence.migrations import MigrationRunner, default_migrations_dir
from pulpmill.pipeline.context import Application
from pulpmill.pipeline.runner import PipelineRunner

PLATFORM = "synthetic"

BODY = (
    "I had been covering my roommate's share of the rent for four months before I finally "
    "said something. They kept telling me work had dried up and I kept telling myself it "
    "was temporary.\n\n"
    'Then I came home early and found a new console in the living room. "It was a gift," '
    "they said, without looking up. We argued for an hour and I said things I regret.\n\n"
    "Eventually they admitted they had been spending the rent money for months. I told them "
    "to move out by the end of the month. Now their family calls me every day telling me I "
    "ruined everything, and I genuinely cannot tell any more whether I overreacted."
)


class SyntheticAdapter:
    """A source with no network, driven entirely by the payloads handed to it."""

    #: Records the adapter's own view of what it was asked for, per instance.
    payloads: list[dict[str, object]] = []
    available: bool = True
    fail_with: Exception | None = None
    normalize_error_ids: set[str] = set()

    def __init__(self, context: AdapterContext) -> None:
        self._context = context
        self._clock = context.clock

    @property
    def platform(self) -> str:
        return PLATFORM

    def health(self) -> AdapterHealth:
        if not type(self).available:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail="synthetic source disabled for this test",
                remediation="flip SyntheticAdapter.available",
            )
        return AdapterHealth(platform=PLATFORM, available=True, detail="ready")

    def fetch(self, request: FetchRequest) -> Iterator[RawStory]:
        if type(self).fail_with is not None:
            raise type(self).fail_with
        for index, payload in enumerate(type(self).payloads):
            if index >= request.limit:
                return
            yield RawStory(
                source_platform=PLATFORM,
                source_id=str(payload["source_id"]),
                canonical_url=str(payload["url"]),
                fetched_at=self._clock.now(),
                payload=payload,
                retrieval={"query": "synthetic"},
            )

    def normalize(self, raw: RawStory) -> Story | None:
        if raw.source_id in type(self).normalize_error_ids:
            raise SourceResponseError("synthetic malformed payload", source=PLATFORM)
        body = str(raw.payload.get("body") or "")
        if not body:
            return None
        return build_story(
            platform=PLATFORM,
            source_id=raw.source_id,
            canonical_url=raw.canonical_url,
            title=str(raw.payload["title"]),
            raw_content=body,
            normalized_content=body,
            created_at=raw.payload.get("created_at") or datetime(2026, 8, 1, tzinfo=UTC),
            discovered_at=raw.fetched_at,
            author=str(raw.payload.get("author") or "anon"),
            engagement=Engagement(
                score=raw.payload.get("score"), comments=raw.payload.get("comments")
            ),
            metadata={
                QUALITY_KEY: "synthetic-community",
                RAW_FORMAT_KEY: "plain",
            },
            simhash_min_tokens=(
                self._context.config.deduplication.layers.near_duplicate.min_tokens
            ),
        )

    def close(self) -> None:
        return None


register_adapter(PLATFORM, SyntheticAdapter)


#: Three genuinely unrelated stories. Sharing a stem would (correctly) trip the
#: near-duplicate layer, so anything testing "N distinct stories" must use these.
DISTINCT_BODIES = [
    (
        "My grandmother left me a locked writing desk when she died and it took me two years "
        "to find the key taped under a drawer in her old sewing room.\n\n"
        "Inside were forty letters, all addressed to a man none of us had ever heard of, all "
        "written after my grandfather died. She never posted a single one.\n\n"
        "I read three and then stopped. It felt like standing in a room I had not been "
        "invited into. My mother wants me to burn them and my sister wants them published."
    ),
    (
        "The bakery on my corner has been run by the same couple for thirty years and last "
        "month a sign went up saying they were closing on the fifteenth.\n\n"
        "I went in every single day of those last two weeks. On the final morning there was "
        "a queue down the block, people who had clearly not shopped there in years.\n\n"
        "The husband cried when he pulled the shutter down. I have never seen a street go "
        "that quiet. Somebody started clapping and then everybody did."
    ),
    (
        "I took a job three hundred miles from home because the salary was almost double and "
        "I told myself the distance would not matter.\n\n"
        "Eight months in I realised I had not had a conversation outside work since March. "
        "My flat is nicer than anything I could afford before and I hate being in it.\n\n"
        "Last week I got promoted and my first thought was that now I would have to stay. "
        "I handed in my notice on Tuesday and I still cannot explain it to my parents."
    ),
]


def payload(
    source_id: str,
    *,
    title: str = "AITA for telling my roommate to move out over unpaid rent?",
    body: str | None = BODY,
    url: str | None = None,
    score: int | None = 5000,
    comments: int | None = 800,
    created_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "source_id": source_id,
        "url": url or f"https://synthetic.example/story/{source_id}",
        "title": title,
        "body": body,
        "score": score,
        "comments": comments,
        "created_at": created_at or datetime(2026, 8, 1, 6, 0, tzinfo=UTC),
    }


@pytest.fixture
def synthetic_config(config: AppConfig) -> AppConfig:
    """The real config plus one synthetic source, with the others switched off."""
    sources = {
        name: source.model_copy(update={"enabled": False})
        for name, source in config.sources.items()
    }
    sources[PLATFORM] = SourceConfig(
        adapter=PLATFORM,
        enabled=True,
        quality=0.8,
        quality_overrides={"synthetic-community": 0.9},
        engagement={"score_reference": 4000.0, "comment_reference": 300.0},
        queries=({"query": "synthetic"},),
    )
    return config.model_copy(update={"sources": sources})


@pytest.fixture
def pipeline(synthetic_config: AppConfig, app: Application, clock) -> PipelineRunner:
    app.config = synthetic_config
    app.ranking = type(app.ranking)(synthetic_config)
    return PipelineRunner(app)


@pytest.fixture(autouse=True)
def reset_adapter() -> Iterator[None]:
    SyntheticAdapter.payloads = []
    SyntheticAdapter.available = True
    SyntheticAdapter.fail_with = None
    SyntheticAdapter.normalize_error_ids = set()
    yield
    SyntheticAdapter.payloads = []
    SyntheticAdapter.available = True
    SyntheticAdapter.fail_with = None
    SyntheticAdapter.normalize_error_ids = set()


class TestRegistry:
    def test_the_synthetic_adapter_registered_without_core_changes(self) -> None:
        assert PLATFORM in registered_adapters()
        assert {"reddit", "fourchan", "x"} <= set(registered_adapters())

    def test_an_unknown_adapter_name_is_reported_clearly(self, config, secrets, clock) -> None:
        from pulpmill.domain.errors import UnknownSourceError
        from pulpmill.ingestion.registry import create_adapter

        broken = SourceConfig(adapter="does-not-exist")
        with pytest.raises(UnknownSourceError, match="does-not-exist"):
            create_adapter(
                AdapterContext(
                    name="broken",
                    config=config,
                    source_config=broken,
                    secrets=secrets,
                    clock=clock,
                )
            )


class TestFullRun:
    def test_the_whole_slice_runs_end_to_end(self, pipeline: PipelineRunner, app) -> None:
        SyntheticAdapter.payloads = [
            payload(f"s{index}", body=body, title=f"Story number {index}")
            for index, body in enumerate(DISTINCT_BODIES)
        ]

        report = pipeline.run()

        assert report.ingest.fetched == 3
        assert report.ingest.new == 3
        assert report.ingest.failures == 0
        assert report.rank.ranked == 3

        candidates = app.rankings.top_candidates(
            ranking_version=app.ranking.version,
            config_fingerprint=app.ranking.config_fingerprint,
            limit=10,
        )
        assert len(candidates) == 3
        assert all(entry.story.status is StoryStatus.RANKED for entry in candidates)

    def test_provenance_survives_the_entire_pipeline(self, pipeline: PipelineRunner, app) -> None:
        """Video -> part -> story -> source -> URL has to hold from the start."""
        SyntheticAdapter.payloads = [
            payload("keepme", url="https://synthetic.example/story/keepme?utm_source=feed")
        ]
        pipeline.run()

        candidate = app.rankings.top_candidates(
            ranking_version=app.ranking.version,
            config_fingerprint=app.ranking.config_fingerprint,
            limit=1,
        )[0]
        story = candidate.story
        assert story.source_platform == PLATFORM
        assert story.source_id == "keepme"
        # Stored byte-identical, tracking parameter and all.
        assert story.canonical_url == "https://synthetic.example/story/keepme?utm_source=feed"
        assert story.author == "anon"
        assert story.provenance.canonical_url == story.canonical_url

    def test_state_transitions_are_recorded_for_every_story(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.run()
        story_id = next(iter(app.stories.iter_by_status((StoryStatus.RANKED,)))).id
        assert [event["to_status"] for event in app.stories.history(story_id)] == [
            "DISCOVERED",
            "NORMALIZED",
            "DEDUPLICATED",
            "RANKED",
        ]

    def test_unusable_records_are_filtered_not_failed(self, pipeline: PipelineRunner) -> None:
        SyntheticAdapter.payloads = [payload("good"), payload("empty", body=None)]
        report = pipeline.run()
        assert report.ingest.fetched == 2
        assert report.ingest.new == 1
        assert report.ingest.filtered == 1
        assert report.ingest.failures == 0

    def test_the_fetch_limit_is_honoured(self, pipeline: PipelineRunner) -> None:
        SyntheticAdapter.payloads = [payload(f"s{index}") for index in range(50)]
        report = pipeline.run(limit=5)
        assert report.ingest.fetched == 5

    def test_a_job_record_is_written_for_each_stage(self, pipeline: PipelineRunner, app) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.run()
        kinds = {job.kind: job for job in app.jobs.recent(10)}
        assert set(kinds) == {"ingest", "rank"}
        assert all(job.status is JobStatus.SUCCEEDED for job in kinds.values())
        assert kinds["ingest"].stats["new"] == 1


class TestIdempotency:
    def test_running_twice_creates_one_story(self, pipeline: PipelineRunner, app) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        first = pipeline.run()
        second = pipeline.run()

        assert first.ingest.new == 1
        assert second.ingest.new == 0
        assert second.ingest.known == 1
        assert app.stories.count_all() == 1

    def test_re_ranking_is_skipped_by_default(self, pipeline: PipelineRunner, app) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.run()
        report = pipeline.rank()
        assert report.skipped == 1
        assert report.ranked == 0
        assert app.rankings.count() == 1

    def test_forced_re_ranking_updates_in_place(self, pipeline: PipelineRunner, app) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.run()
        report = pipeline.rank(force=True)
        assert report.ranked == 1
        assert app.rankings.count() == 1

    def test_re_running_produces_the_same_ranking(
        self, pipeline: PipelineRunner, app, clock
    ) -> None:
        """Same input, same config, same reference time -> same score."""
        SyntheticAdapter.payloads = [
            payload(f"s{index}", body=body, title=f"Story number {index}")
            for index, body in enumerate(DISTINCT_BODIES)
        ]
        reference = clock.now()
        pipeline.ingest()
        pipeline.rank(reference_time=reference)

        def scores() -> dict[str, float]:
            return {
                entry.story.id: entry.ranking.final_score
                for entry in app.rankings.top_candidates(
                    ranking_version=app.ranking.version,
                    config_fingerprint=app.ranking.config_fingerprint,
                    limit=10,
                )
            }

        before = scores()
        pipeline.rank(reference_time=reference, force=True)
        assert scores() == before


class TestDeduplicationInTheRun:
    def test_a_cross_source_repost_is_marked_not_re_ingested(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.payloads = [
            payload("original"),
            payload("repost", url="https://synthetic.example/story/repost"),
        ]
        report = pipeline.run()

        assert report.ingest.new == 1
        assert report.ingest.duplicates == 1
        duplicates = list(app.stories.iter_by_status((StoryStatus.DUPLICATE,)))
        assert len(duplicates) == 1
        assert duplicates[0].duplicate_of_id is not None
        assert duplicates[0].duplicate_layer is not None

    def test_duplicates_are_excluded_from_the_candidate_list(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.payloads = [payload("original"), payload("repost")]
        pipeline.run()
        candidates = app.rankings.top_candidates(
            ranking_version=app.ranking.version,
            config_fingerprint=app.ranking.config_fingerprint,
            limit=10,
        )
        assert len(candidates) == 1

    def test_the_dedup_sweep_can_be_re_run(self, pipeline: PipelineRunner) -> None:
        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.run()
        stats = pipeline.rededuplicate()
        assert stats["examined"] == 1
        assert stats["marked"] == 0
        assert stats["failures"] == 0


class TestFailureHandling:
    def test_an_unavailable_source_is_skipped_not_fatal(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.available = False
        SyntheticAdapter.payloads = [payload("s1")]

        report = pipeline.ingest()
        source = report.sources[PLATFORM]
        assert source.available is False
        assert source.remediation
        assert report.failures == 0  # a config state, not an error
        assert app.stories.count_all() == 0

    def test_a_fetch_failure_is_recorded_and_the_run_completes(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.fail_with = IngestionError("upstream exploded", source=PLATFORM)
        report = pipeline.ingest()

        assert report.failures == 1
        recorded = app.failures.recent(1)[0]
        assert recorded["stage"] == "fetch"
        assert recorded["source_platform"] == PLATFORM
        assert recorded["error_type"] == "IngestionError"
        # The job still finished cleanly rather than crashing the worker.
        assert app.jobs.recent(1)[0].status is JobStatus.SUCCEEDED

    def test_source_unavailable_mid_fetch_is_not_a_failure(self, pipeline: PipelineRunner) -> None:
        SyntheticAdapter.fail_with = SourceUnavailableError("credentials revoked")
        report = pipeline.ingest()
        assert report.sources[PLATFORM].available is False
        assert report.failures == 0

    def test_one_malformed_record_does_not_stop_the_others(
        self, pipeline: PipelineRunner, app
    ) -> None:
        SyntheticAdapter.payloads = [
            payload("bad", body=DISTINCT_BODIES[0]),
            payload("good", body=DISTINCT_BODIES[1]),
        ]
        SyntheticAdapter.normalize_error_ids = {"bad"}

        report = pipeline.ingest()
        assert report.failures == 1
        assert report.new == 1
        assert app.failures.recent(1)[0]["stage"] == "normalize"

    def test_an_unknown_source_name_is_rejected_before_any_work(
        self, pipeline: PipelineRunner
    ) -> None:
        with pytest.raises(IngestionError, match="no such source"):
            pipeline.ingest(sources=["not-configured"])

    def test_a_disabled_source_can_still_be_named_explicitly(
        self, pipeline: PipelineRunner, app
    ) -> None:
        """`--source x` must work for testing a disabled adapter."""
        report = pipeline.ingest(sources=["x"])
        assert "x" in report.sources
        assert report.sources["x"].available is False


class TestRestartSurvival:
    def test_work_persists_across_a_process_restart(
        self, synthetic_config: AppConfig, clock, secrets
    ) -> None:
        SyntheticAdapter.payloads = [
            payload(f"s{index}", body=body, title=f"Story number {index}")
            for index, body in enumerate(DISTINCT_BODIES)
        ]

        # --- first "process": ingest only, then die ---
        first_db = Database(synthetic_config.database_path, synthetic_config.runtime.database)
        MigrationRunner(first_db, default_migrations_dir(synthetic_config.project_root)).upgrade()
        first_app = Application(synthetic_config, secrets=secrets, clock=clock, database=first_db)
        PipelineRunner(first_app).ingest()
        assert first_app.stories.count_all() == 3
        first_db.close()

        # --- second "process": pick up exactly where the first stopped ---
        second_db = Database(synthetic_config.database_path, synthetic_config.runtime.database)
        second_app = Application(synthetic_config, secrets=secrets, clock=clock, database=second_db)
        assert second_app.stories.count_all() == 3

        report = PipelineRunner(second_app).rank()
        assert report.ranked == 3
        assert (
            len(
                second_app.rankings.top_candidates(
                    ranking_version=second_app.ranking.version,
                    config_fingerprint=second_app.ranking.config_fingerprint,
                    limit=10,
                )
            )
            == 3
        )
        second_db.close()

    def test_an_abandoned_job_is_reclaimed_on_the_next_run(
        self, pipeline: PipelineRunner, app, clock
    ) -> None:
        app.jobs.start("ingest", {"note": "process killed here"})
        clock.advance(timedelta(hours=8).total_seconds())

        SyntheticAdapter.payloads = [payload("s1")]
        pipeline.ingest()

        statuses = {job.status for job in app.jobs.recent(10)}
        assert JobStatus.INTERRUPTED in statuses
        assert app.jobs.count_running() == 0
