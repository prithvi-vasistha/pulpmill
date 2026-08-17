"""The command-line interface.

Driven through Typer's runner against a temporary config with every source
disabled, so no command in this file can reach the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from pulpmill.cli.app import app
from pulpmill.config.models import AppConfig
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.persistence.database import Database
from pulpmill.persistence.migrations import MigrationRunner, default_migrations_dir
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import StoryRepository
from pulpmill.ranking.engine import RankingEngine
from tests.support.clock import ManualClock

runner = CliRunner()


@pytest.fixture
def cli_config(project_root: Path, tmp_path: Path) -> Path:
    """A config file with all sources off and the database under tmp."""
    data = yaml.safe_load((project_root / "config" / "pipeline.yaml").read_text())
    for source in data["sources"].values():
        source["enabled"] = False
    data["runtime"]["data_dir"] = str(tmp_path)
    data["runtime"]["database"]["path"] = str(tmp_path / "pulpmill.db")
    data["runtime"]["logging"]["file"]["enabled"] = False
    data["runtime"]["logging"]["level"] = "ERROR"

    path = tmp_path / "pipeline.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def invoke(cli_config: Path, *args: str):
    return runner.invoke(app, ["--config", str(cli_config), *args])


@pytest.fixture
def seeded(cli_config: Path, project_root: Path, make_story) -> Iterator[list[str]]:
    """Three ranked stories already in the CLI's database."""
    from pulpmill.config.loader import load_config

    config: AppConfig = load_config(
        project_root=project_root, config_path=cli_config, environ={}, load_dotenv=False
    )
    database = Database(config.database_path, config.runtime.database)
    MigrationRunner(database, default_migrations_dir(project_root)).upgrade()

    clock = ManualClock()
    stories = StoryRepository(database, clock)
    rankings = RankingRepository(database, clock)
    engine = RankingEngine(config)

    ids: list[str] = []
    for index in range(3):
        story = make_story(
            source_id=f"t3_cli{index}",
            canonical_url=f"https://www.reddit.com/r/x/comments/cli{index}/",
            score=(index + 1) * 5000,
        )
        stories.upsert(story)
        stories.transition(story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.transition(story.id, StoryStatus.DEDUPLICATED, stage=PipelineStage.DEDUPLICATE)
        rankings.save(engine.rank(story, reference_time=clock.now()))
        stories.transition(story.id, StoryStatus.RANKED, stage=PipelineStage.RANK)
        ids.append(story.id)

    database.close()
    yield ids


class TestTopLevel:
    def test_version(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "pulpmill" in result.stdout

    def test_help_lists_only_working_commands(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in (
            "run",
            "scrape",
            "rank",
            "dedupe",
            "renormalize",
            "select",
            "sources",
            "status",
            "top",
            "inspect",
            "failures",
            "db",
            "config",
        ):
            assert command in result.stdout

    def test_a_missing_config_file_exits_cleanly(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["--config", str(tmp_path / "nope.yaml"), "status"])
        assert result.exit_code == 2
        assert "not found" in result.output


class TestSchemaCommands:
    def test_db_upgrade_then_status(self, cli_config: Path) -> None:
        upgrade = invoke(cli_config, "db", "upgrade")
        assert upgrade.exit_code == 0
        assert "0001_initial" in upgrade.stdout

        status = invoke(cli_config, "db", "status")
        assert status.exit_code == 0
        assert "applied" in status.stdout

    def test_db_upgrade_is_idempotent(self, cli_config: Path) -> None:
        invoke(cli_config, "db", "upgrade")
        second = invoke(cli_config, "db", "upgrade")
        assert second.exit_code == 0
        assert "up to date" in second.stdout

    def test_db_verify_passes_on_a_fresh_database(self, cli_config: Path) -> None:
        invoke(cli_config, "db", "upgrade")
        result = invoke(cli_config, "db", "verify")
        assert result.exit_code == 0


class TestConfigCommands:
    def test_config_show(self, cli_config: Path) -> None:
        result = invoke(cli_config, "config", "show")
        assert result.exit_code == 0
        assert "ranking version" in result.stdout
        assert "config fingerprint" in result.stdout

    def test_config_show_json_is_parseable(self, cli_config: Path) -> None:
        result = invoke(cli_config, "config", "show", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ranking"]["version"]
        assert "sources" in payload

    def test_config_secrets_reports_presence_without_values(self, cli_config: Path) -> None:
        result = invoke(cli_config, "config", "secrets")
        assert result.exit_code == 0
        assert "PULPMILL_REDDIT_CLIENT_ID" in result.stdout
        assert "ANTHROPIC_API_KEY" in result.stdout
        assert "never read back" in result.stdout


class TestSources:
    def test_sources_reports_health_without_network(self, cli_config: Path) -> None:
        result = invoke(cli_config, "sources")
        assert result.exit_code == 0
        for name in ("reddit", "fourchan", "x"):
            assert name in result.stdout
        # Reddit has no credentials in the test environment.
        assert "unavailable" in result.stdout

    def test_registered_adapters_are_listed(self, cli_config: Path) -> None:
        result = invoke(cli_config, "sources")
        assert "registered adapters" in result.stdout


class TestStatusAndCandidates:
    def test_status_on_an_empty_database(self, cli_config: Path) -> None:
        result = invoke(cli_config, "status")
        assert result.exit_code == 0
        assert "pulpmill status" in result.stdout

    def test_status_json_is_parseable(self, cli_config: Path, seeded: list[str]) -> None:
        result = invoke(cli_config, "status", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["stories_total"] == 3
        assert payload["stories_by_status"]["RANKED"] == 3
        assert payload["stories_by_source"]["reddit"] == 3
        assert len(payload["top_candidates"]) == 3

    def test_top_shows_candidates_highest_first(self, cli_config: Path, seeded: list[str]) -> None:
        result = invoke(cli_config, "top", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        scores = [entry["score"] for entry in payload]
        assert scores == sorted(scores, reverse=True)

    def test_top_reports_clearly_when_nothing_is_ranked(self, cli_config: Path) -> None:
        result = invoke(cli_config, "top")
        assert result.exit_code == 1
        assert "No ranked stories" in result.output

    def test_top_can_filter_by_source(self, cli_config: Path, seeded: list[str]) -> None:
        assert len(json.loads(invoke(cli_config, "top", "-s", "reddit", "--json").stdout)) == 3
        result = invoke(cli_config, "top", "-s", "fourchan")
        assert result.exit_code == 1


class TestInspect:
    def test_inspect_shows_provenance_and_the_score_breakdown(
        self, cli_config: Path, seeded: list[str]
    ) -> None:
        result = invoke(cli_config, "inspect", seeded[0])
        assert result.exit_code == 0
        assert "Ranking breakdown" in result.stdout
        assert "State history" in result.stdout
        assert "narrative_suitability" in result.stdout

    def test_inspect_json_carries_full_provenance(
        self, cli_config: Path, seeded: list[str]
    ) -> None:
        result = invoke(cli_config, "inspect", seeded[0], "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        story = payload["story"]
        assert story["source_platform"] == "reddit"
        assert story["source_id"].startswith("t3_")
        assert story["canonical_url"].startswith("https://www.reddit.com/")
        assert payload["ranking"]["component_scores"]
        assert payload["history"]

    def test_an_unknown_story_id_exits_non_zero(self, cli_config: Path) -> None:
        result = invoke(cli_config, "inspect", "00000000-0000-0000-0000-000000000000")
        assert result.exit_code == 1
        assert "no story" in result.output


class TestPipelineCommands:
    def test_run_with_no_enabled_sources_succeeds_quietly(self, cli_config: Path) -> None:
        result = invoke(cli_config, "run")
        assert result.exit_code == 0
        assert "No ranked candidates" in result.stdout

    def test_run_json_output_is_parseable(self, cli_config: Path) -> None:
        result = invoke(cli_config, "run", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ingest"]["fetched"] == 0
        assert "rank" in payload

    def test_rank_reports_skipping_already_scored_stories(
        self, cli_config: Path, seeded: list[str]
    ) -> None:
        result = invoke(cli_config, "rank", "--json")
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["skipped"] == 3
        assert payload["ranked"] == 0

    def test_dedupe_sweep_runs_over_stored_stories(
        self, cli_config: Path, seeded: list[str]
    ) -> None:
        result = invoke(cli_config, "dedupe")
        assert result.exit_code == 0
        assert "examined" in result.stdout

    def test_renormalize_dry_run_writes_nothing(self, cli_config: Path, seeded: list[str]) -> None:
        result = invoke(cli_config, "renormalize", "--dry-run")
        assert result.exit_code == 0
        assert "dry run" in result.stdout

    def test_scrape_with_an_unknown_source_exits_non_zero(self, cli_config: Path) -> None:
        result = invoke(cli_config, "scrape", "--source", "not-a-source")
        assert result.exit_code == 1
        assert "no such source" in result.output


class TestSelect:
    def test_select_uses_the_deterministic_provider_by_default(
        self, cli_config: Path, seeded: list[str]
    ) -> None:
        result = invoke(cli_config, "select", "--count", "2")
        assert result.exit_code == 0
        assert "deterministic" in result.stdout
        assert "Batch" in result.stdout

    def test_select_without_ranked_stories_exits_non_zero(self, cli_config: Path) -> None:
        result = invoke(cli_config, "select")
        assert result.exit_code == 1
        assert "Nothing ranked" in result.output

    def test_requesting_claude_without_a_key_falls_back_and_says_so(
        self, cli_config: Path, seeded: list[str], monkeypatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = invoke(cli_config, "select", "--provider", "claude", "--count", "2")
        assert result.exit_code == 0
        assert "unavailable" in result.stdout
        assert "deterministic" in result.stdout
