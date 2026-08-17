"""Migrations, repositories, idempotency and restart safety."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from pulpmill.config.models import AppConfig
from pulpmill.domain.enums import DedupLayer, JobStatus, PipelineStage, StoryStatus
from pulpmill.domain.errors import InvalidStateTransitionError, MigrationError, StoryNotFoundError
from pulpmill.persistence.database import Database
from pulpmill.persistence.migrations import (
    MigrationRunner,
    default_migrations_dir,
    discover_migrations,
    split_sql_statements,
)
from pulpmill.persistence.repositories.jobs import FailureRecord, FailureRepository, JobRepository
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import StoryRepository
from pulpmill.ranking.engine import RankingEngine
from tests.support.clock import ManualClock


class TestMigrations:
    def test_upgrade_creates_every_table(self, config: AppConfig) -> None:
        db = Database(config.database_path, config.runtime.database)
        MigrationRunner(db, default_migrations_dir(config.project_root)).upgrade()
        tables = set(db.table_names())
        assert {
            "stories",
            "story_rankings",
            "story_state_events",
            "story_simhash_bands",
            "jobs",
            "job_failures",
            "editorial_batches",
            "editorial_selections",
            "story_series",
            "story_parts",
            "schema_migrations",
        } <= tables
        db.close()

    def test_upgrade_is_idempotent(self, config: AppConfig) -> None:
        db = Database(config.database_path, config.runtime.database)
        runner = MigrationRunner(db, default_migrations_dir(config.project_root))
        assert runner.upgrade() == ["0001_initial"]
        assert runner.upgrade() == []
        assert runner.status().is_current
        db.close()

    def test_an_edited_applied_migration_is_refused(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        """Two machines must not silently end up with different schemas."""
        directory = tmp_path / "migrations"
        directory.mkdir()
        path = directory / "0001_initial.sql"
        path.write_text("CREATE TABLE demo (id INTEGER PRIMARY KEY);")

        db = Database(config.database_path, config.runtime.database)
        MigrationRunner(db, directory).upgrade()

        path.write_text("CREATE TABLE demo (id INTEGER PRIMARY KEY, extra TEXT);")
        with pytest.raises(MigrationError, match="has been modified"):
            MigrationRunner(db, directory).upgrade()
        db.close()

    def test_a_failed_migration_leaves_the_schema_at_the_last_good_version(
        self, config: AppConfig, tmp_path: Path
    ) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_ok.sql").write_text("CREATE TABLE good (id INTEGER PRIMARY KEY);")
        (directory / "0002_broken.sql").write_text(
            "CREATE TABLE later (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;"
        )

        db = Database(config.database_path, config.runtime.database)
        with pytest.raises(MigrationError, match="0002_broken"):
            MigrationRunner(db, directory).upgrade()

        tables = db.table_names()
        assert "good" in tables
        # The broken migration's first statement must have been rolled back.
        assert "later" not in tables
        db.close()

    @pytest.mark.parametrize(
        "filename", ["1_bad.sql", "0001-bad.sql", "0001_Bad.sql", "initial.sql"]
    )
    def test_filenames_must_follow_the_convention(self, tmp_path: Path, filename: str) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / filename).write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="filename"):
            discover_migrations(directory)

    def test_versions_must_be_consecutive(self, tmp_path: Path) -> None:
        directory = tmp_path / "migrations"
        directory.mkdir()
        (directory / "0001_a.sql").write_text("SELECT 1;")
        (directory / "0003_c.sql").write_text("SELECT 1;")
        with pytest.raises(MigrationError, match="consecutive"):
            discover_migrations(directory)

    def test_statement_splitting_respects_literals_and_comments(self) -> None:
        script = """
        -- a comment with a ; semicolon
        CREATE TABLE t (a TEXT DEFAULT 'x;y');
        INSERT INTO t VALUES ('one; two');
        """
        statements = split_sql_statements(script)
        assert len(statements) == 2
        assert "CREATE TABLE" in statements[0]
        assert "INSERT INTO" in statements[1]

    def test_an_unterminated_statement_is_reported(self) -> None:
        with pytest.raises(MigrationError, match="incomplete statement"):
            split_sql_statements("CREATE TABLE t (a TEXT)")

    def test_the_real_migrations_parse(self, project_root: Path) -> None:
        for migration in discover_migrations(default_migrations_dir(project_root)):
            assert migration.statements


class TestStoryPersistence:
    def test_insert_then_read_back_preserves_every_field(
        self, stories: StoryRepository, make_story
    ) -> None:
        story = make_story()
        result = stories.upsert(story)
        assert result.created is True

        loaded = stories.require(story.id)
        assert loaded.source_platform == story.source_platform
        assert loaded.source_id == story.source_id
        assert loaded.canonical_url == story.canonical_url
        assert loaded.author == story.author
        assert loaded.title == story.title
        assert loaded.normalized_content == story.normalized_content
        assert loaded.content_hash == story.content_hash
        assert loaded.simhash == story.simhash
        assert loaded.engagement == story.engagement
        assert dict(loaded.metadata) == dict(story.metadata)
        assert loaded.created_at == story.created_at

    def test_scraping_the_same_post_twice_yields_one_story(
        self, stories: StoryRepository, make_story
    ) -> None:
        story = make_story()
        assert stories.upsert(story).created is True
        assert stories.upsert(story).created is False
        assert stories.count_all() == 1

    def test_ids_are_derived_from_the_source_pair(self, make_story) -> None:
        assert make_story(source_id="t3_same").id == make_story(source_id="t3_same").id
        assert make_story(source_id="t3_a").id != make_story(source_id="t3_b").id

    def test_a_re_scrape_refreshes_counters_without_resetting_progress(
        self, stories: StoryRepository, make_story
    ) -> None:
        story = make_story(score=100)
        stories.upsert(story)
        stories.transition(story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.transition(story.id, StoryStatus.DEDUPLICATED, stage=PipelineStage.DEDUPLICATE)

        refreshed = make_story(source_id=story.source_id, score=9000)
        result = stories.upsert(refreshed)

        assert result.created is False
        assert result.updated is True
        assert result.story.engagement.score == 9000
        # Pipeline position and discovery time are untouched.
        assert result.story.status is StoryStatus.DEDUPLICATED
        assert result.story.discovered_at == story.discovered_at

    def test_an_unchanged_re_scrape_is_a_no_op(self, stories: StoryRepository, make_story) -> None:
        story = make_story()
        stories.upsert(story)
        before = stories.require(story.id).updated_at
        result = stories.upsert(story)
        assert result.updated is False
        assert stories.require(story.id).updated_at == before

    def test_provenance_cannot_be_modified(self, make_story) -> None:
        story = make_story()
        with pytest.raises(ValueError, match="provenance"):
            story.evolve(canonical_url="https://evil.example/hijacked")
        with pytest.raises(ValueError, match="provenance"):
            story.evolve(source_id="t3_other")

    def test_the_source_pair_is_unique_at_the_database_level(
        self, database: Database, stories: StoryRepository, make_story
    ) -> None:
        story = make_story()
        stories.upsert(story)
        with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
            connection.execute(
                "INSERT INTO stories (id, source_platform, source_id, canonical_url, "
                "url_fingerprint, title, raw_content, normalized_content, content_hash, "
                "word_count, created_at, discovered_at, updated_at, engagement_json, "
                "metadata_json, status) "
                "VALUES ('other-id', ?, ?, 'u', 'f', 't', 'r', 'n', 'h', 1, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'{}', '{}', 'DISCOVERED')",
                (story.source_platform, story.source_id),
            )

    def test_an_unknown_status_is_rejected_by_the_schema(self, database: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
            connection.execute(
                "INSERT INTO stories (id, source_platform, source_id, canonical_url, "
                "url_fingerprint, title, raw_content, normalized_content, content_hash, "
                "word_count, created_at, discovered_at, updated_at, engagement_json, "
                "metadata_json, status) VALUES ('i', 'p', 's', 'u', 'f', 't', 'r', 'n', 'h', 1, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', "
                "'{}', '{}', 'NOT_A_STATUS')"
            )

    def test_missing_stories_raise_a_typed_error(self, stories: StoryRepository) -> None:
        assert stories.get("nope") is None
        with pytest.raises(StoryNotFoundError):
            stories.require("nope")


class TestStateTransitions:
    def test_transitions_are_recorded_as_events(self, stories: StoryRepository, make_story) -> None:
        story = make_story()
        stories.upsert(story, job_id="job-1")
        stories.transition(
            story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE, job_id="job-1"
        )
        history = stories.history(story.id)
        assert [event["to_status"] for event in history] == ["DISCOVERED", "NORMALIZED"]
        assert history[-1]["job_id"] == "job-1"

    def test_an_illegal_transition_is_rejected_and_changes_nothing(
        self, stories: StoryRepository, make_story
    ) -> None:
        story = make_story()
        stories.upsert(story)
        with pytest.raises(InvalidStateTransitionError):
            stories.transition(story.id, StoryStatus.PUBLISHED, stage=PipelineStage.PUBLISH)
        assert stories.require(story.id).status is StoryStatus.DISCOVERED
        assert len(stories.history(story.id)) == 1

    def test_marking_a_duplicate_links_and_records_the_layer(
        self, stories: StoryRepository, make_story
    ) -> None:
        original = make_story(source_id="t3_orig")
        duplicate = make_story(source_id="t3_dupe")
        stories.upsert(original)
        stories.upsert(duplicate)
        stories.transition(duplicate.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)

        marked = stories.mark_duplicate(
            duplicate.id, duplicate_of_id=original.id, layer=DedupLayer.CONTENT_HASH
        )
        assert marked.status is StoryStatus.DUPLICATE
        assert marked.duplicate_of_id == original.id
        assert marked.duplicate_layer is DedupLayer.CONTENT_HASH

    def test_a_duplicate_leaves_the_near_duplicate_index(
        self, database: Database, stories: StoryRepository, make_story
    ) -> None:
        """Otherwise a duplicate keeps matching later stories."""
        original = make_story(source_id="t3_orig")
        duplicate = make_story(source_id="t3_dupe")
        stories.upsert(original)
        stories.upsert(duplicate)
        stories.transition(duplicate.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.mark_duplicate(
            duplicate.id, duplicate_of_id=original.id, layer=DedupLayer.NEAR_DUPLICATE
        )
        remaining = database.query_scalar(
            "SELECT count(*) FROM story_simhash_bands WHERE story_id = ?", (duplicate.id,)
        )
        assert remaining == 0

    def test_a_story_cannot_duplicate_itself(self, stories: StoryRepository, make_story) -> None:
        story = make_story()
        stories.upsert(story)
        with pytest.raises(ValueError, match="itself"):
            stories.mark_duplicate(
                story.id, duplicate_of_id=story.id, layer=DedupLayer.CONTENT_HASH
            )


class TestRankingPersistence:
    def _rank(self, config: AppConfig, story, clock: ManualClock):
        return RankingEngine(config).rank(story, reference_time=clock.now())

    def test_rankings_round_trip_with_their_explanation(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        story = make_story()
        stories.upsert(story)
        result = self._rank(config, story, clock)
        rankings.save(result)

        loaded = rankings.get(
            story.id,
            ranking_version=result.ranking_version,
            config_fingerprint=result.config_fingerprint,
        )
        assert loaded is not None
        assert loaded.final_score == result.final_score
        assert dict(loaded.component_scores) == dict(result.component_scores)
        assert loaded.explanation["signals"].keys() == result.explanation["signals"].keys()
        assert loaded.reference_time == result.reference_time

    def test_re_ranking_updates_rather_than_duplicating(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        story = make_story()
        stories.upsert(story)
        result = self._rank(config, story, clock)
        rankings.save(result)
        rankings.save(result)
        assert rankings.count() == 1

    def test_a_different_config_produces_a_second_row(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        """Old scores stay attributable to the config that produced them."""
        story = make_story()
        stories.upsert(story)
        rankings.save(self._rank(config, story, clock))

        weights = config.ranking.weights.model_copy(update={"engagement": 0.9})
        tweaked = config.model_copy(
            update={"ranking": config.ranking.model_copy(update={"weights": weights})}
        )
        rankings.save(self._rank(tweaked, story, clock))
        assert rankings.count() == 2

    def test_candidates_come_back_highest_first(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        for index in range(5):
            story = make_story(source_id=f"t3_c{index}", score=index * 3000)
            stories.upsert(story)
            rankings.save(self._rank(config, story, clock))

        candidates = rankings.top_candidates(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            limit=10,
        )
        scores = [entry.ranking.final_score for entry in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_candidate_ordering_is_total_and_repeatable(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        """Identical scores must still produce one stable order."""
        for index in range(6):
            story = make_story(source_id=f"t3_tie{index}")
            stories.upsert(story)
            rankings.save(self._rank(config, story, clock))

        def ids() -> list[str]:
            return [
                entry.story.id
                for entry in rankings.top_candidates(
                    ranking_version=config.ranking.version,
                    config_fingerprint=config.ranking.fingerprint(),
                    limit=10,
                )
            ]

        assert ids() == ids()

    def test_duplicates_are_excluded_from_candidates(
        self, config, stories, rankings: RankingRepository, make_story, clock
    ) -> None:
        original = make_story(source_id="t3_keep")
        duplicate = make_story(source_id="t3_drop")
        for story in (original, duplicate):
            stories.upsert(story)
            rankings.save(self._rank(config, story, clock))
        stories.transition(duplicate.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.mark_duplicate(
            duplicate.id, duplicate_of_id=original.id, layer=DedupLayer.CONTENT_HASH
        )

        ids = {
            entry.story.id
            for entry in rankings.top_candidates(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
                limit=10,
            )
        }
        assert duplicate.id not in ids
        assert original.id in ids


class TestJobsAndFailures:
    def test_a_job_is_visible_while_it_runs(self, jobs: JobRepository) -> None:
        """Committed before any work, so a hard kill still leaves evidence."""
        job_id = jobs.start("ingest", {"sources": ["fourchan"]})
        record = jobs.get(job_id)
        assert record is not None
        assert record.status is JobStatus.RUNNING
        assert jobs.count_running() == 1

    def test_finishing_records_stats(self, jobs: JobRepository) -> None:
        job_id = jobs.start("ingest")
        jobs.finish(job_id, status=JobStatus.SUCCEEDED, stats={"new": 7})
        record = jobs.get(job_id)
        assert record is not None
        assert record.status is JobStatus.SUCCEEDED
        assert record.stats["new"] == 7
        assert record.finished_at is not None

    def test_abandoned_jobs_are_reclaimed_after_a_restart(
        self, jobs: JobRepository, clock: ManualClock
    ) -> None:
        job_id = jobs.start("ingest")
        clock.advance(timedelta(hours=8).total_seconds())
        assert jobs.reclaim_stale(older_than=timedelta(hours=6)) == 1
        record = jobs.get(job_id)
        assert record is not None
        assert record.status is JobStatus.INTERRUPTED
        assert record.error

    def test_a_recent_job_is_not_reclaimed(self, jobs: JobRepository, clock: ManualClock) -> None:
        jobs.start("ingest")
        clock.advance(60)
        assert jobs.reclaim_stale(older_than=timedelta(hours=6)) == 0

    def test_failures_record_every_diagnostic_field(
        self, jobs: JobRepository, failures: FailureRepository
    ) -> None:
        job_id = jobs.start("ingest")
        failures.record(
            FailureRecord(
                stage=PipelineStage.FETCH,
                operation="fetch",
                error_type="SourceRequestError",
                error_message="503 from upstream",
                source_platform="reddit",
                story_id="story-1",
                retry_count=4,
                context={"url": "https://oauth.reddit.com/r/x/top"},
            ),
            job_id=job_id,
        )
        row = failures.recent(1)[0]
        assert row["source_platform"] == "reddit"
        assert row["stage"] == "fetch"
        assert row["operation"] == "fetch"
        assert row["error_type"] == "SourceRequestError"
        assert row["retry_count"] == 4
        assert failures.by_stage() == {"fetch": 1}

    def test_long_error_messages_are_bounded(self, failures: FailureRepository) -> None:
        failures.record(
            FailureRecord(
                stage=PipelineStage.RANK,
                operation="rank",
                error_type="ValueError",
                error_message="x" * 10_000,
            )
        )
        assert len(failures.recent(1)[0]["error_message"]) <= 2000


class TestRestartSafety:
    def test_data_survives_closing_and_reopening_the_database(
        self, config: AppConfig, make_story, clock: ManualClock
    ) -> None:
        """The restart guarantee, exercised literally."""
        first = Database(config.database_path, config.runtime.database)
        MigrationRunner(first, default_migrations_dir(config.project_root)).upgrade()
        repo = StoryRepository(first, clock)
        story = make_story()
        repo.upsert(story)
        repo.transition(story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        first.close()

        second = Database(config.database_path, config.runtime.database)
        reopened = StoryRepository(second, clock)
        loaded = reopened.require(story.id)
        assert loaded.status is StoryStatus.NORMALIZED
        assert loaded.canonical_url == story.canonical_url
        assert len(reopened.history(story.id)) == 2
        second.close()

    def test_a_failed_transaction_rolls_back_completely(
        self, database: Database, stories: StoryRepository, make_story
    ) -> None:
        story = make_story()
        stories.upsert(story)
        with pytest.raises(RuntimeError), database.transaction() as connection:
            connection.execute("DELETE FROM stories WHERE id = ?", (story.id,))
            raise RuntimeError("boom")
        assert stories.get(story.id) is not None

    def test_nested_transactions_join_the_outer_one(
        self, database: Database, stories: StoryRepository, make_story
    ) -> None:
        with pytest.raises(RuntimeError), database.transaction():
            stories.upsert(make_story(source_id="t3_nested"))
            raise RuntimeError("boom")
        assert stories.count_all() == 0

    def test_streaming_iteration_returns_every_story_once(
        self, stories: StoryRepository, make_story
    ) -> None:
        """Keyset pagination must not skip or repeat across batch boundaries."""
        expected = set()
        for index in range(25):
            story = make_story(source_id=f"t3_iter{index:03d}")
            stories.upsert(story)
            expected.add(story.id)

        seen = [story.id for story in stories.iter_by_status((StoryStatus.DISCOVERED,))]
        assert len(seen) == len(set(seen)) == 25
        assert set(seen) == expected
