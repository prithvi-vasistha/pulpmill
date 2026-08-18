"""Configuration loading, validation and layering."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pulpmill.config.loader import load_config
from pulpmill.config.models import AppConfig, RankingConfig, deep_merge
from pulpmill.config.secrets import SecretStore, load_env_file, parse_env_file
from pulpmill.domain.errors import ConfigError


class TestConfigLoading:
    def test_committed_config_is_valid(self, project_root: Path) -> None:
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert set(config.sources) == {"reddit", "fourchan", "x"}
        assert config.ranking.version

    def test_x_ships_disabled(self, project_root: Path) -> None:
        """X has no free read tier; it must not be enabled by accident."""
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert config.sources["x"].enabled is False
        assert "x" not in config.enabled_sources()

    def test_unknown_key_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(yaml.safe_dump({"version": 1, "typo_section": {"a": 1}}))
        with pytest.raises(ConfigError, match="typo_section"):
            load_config(project_root=tmp_path, config_path=path, environ={}, load_dotenv=False)

    def test_invalid_value_is_reported_with_its_location(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(
            yaml.safe_dump({"version": 1, "ranking": {"recency": {"half_life_hours": -5}}})
        )
        with pytest.raises(ConfigError) as exc:
            load_config(project_root=tmp_path, config_path=path, environ={}, load_dotenv=False)
        assert "half_life_hours" in str(exc.value)

    def test_missing_file_is_reported_clearly(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(
                project_root=tmp_path,
                config_path=tmp_path / "nope.yaml",
                environ={},
                load_dotenv=False,
            )

    def test_local_overrides_are_layered_over_defaults(
        self, project_root: Path, tmp_path: Path
    ) -> None:
        (tmp_path / "config").mkdir()
        (tmp_path / "pyproject.toml").write_text("")
        base = yaml.safe_load((project_root / "config" / "pipeline.yaml").read_text())
        (tmp_path / "config" / "pipeline.yaml").write_text(yaml.safe_dump(base))
        (tmp_path / "config" / "pipeline.local.yaml").write_text(
            yaml.safe_dump({"ranking": {"weights": {"engagement": 0.99}}})
        )
        config = load_config(project_root=tmp_path, environ={}, load_dotenv=False)
        assert config.ranking.weights.engagement == 0.99
        # Untouched keys keep their committed values.
        assert config.ranking.weights.recency == base["ranking"]["weights"]["recency"]

    def test_environment_overrides_scalars(self, project_root: Path) -> None:
        config = load_config(
            project_root=project_root,
            environ={"PULPMILL_LOG_LEVEL": "DEBUG", "PULPMILL_DATA_DIR": "custom-var"},
            load_dotenv=False,
        )
        assert config.runtime.logging.level == "DEBUG"
        assert config.data_dir == project_root / "custom-var"

    def test_relative_paths_resolve_against_the_project_root(self, tmp_path: Path) -> None:
        config = AppConfig(project_root=tmp_path)
        assert config.database_path == tmp_path / "var" / "pulpmill.db"
        assert config.resolve("/absolute/path") == Path("/absolute/path")


class TestDeepMerge:
    def test_mappings_merge_key_by_key(self) -> None:
        merged = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}})
        assert merged == {"a": {"b": 1, "c": 3}}

    def test_lists_are_replaced_not_appended(self) -> None:
        """An override that names three subreddits must get three, not six."""
        merged = deep_merge({"queries": [1, 2, 3]}, {"queries": [9]})
        assert merged == {"queries": [9]}


class TestRankingConfig:
    def test_fingerprint_changes_when_a_weight_changes(self) -> None:
        base = RankingConfig()
        changed = base.model_copy(
            update={"weights": base.weights.model_copy(update={"engagement": 0.5})}
        )
        assert base.fingerprint() != changed.fingerprint()

    def test_fingerprint_is_stable_for_identical_config(self) -> None:
        assert RankingConfig().fingerprint() == RankingConfig().fingerprint()

    def test_weights_must_not_all_be_zero(self) -> None:
        from pulpmill.config.models import RankingWeights

        with pytest.raises(ValueError, match="greater than zero"):
            RankingWeights(
                engagement=0,
                recency=0,
                comment_activity=0,
                narrative_suitability=0,
                length=0,
                novelty=0,
                source_quality=0,
            )

    def test_length_bounds_must_increase(self) -> None:
        from pulpmill.config.models import LengthConfig

        with pytest.raises(ValueError, match="strictly increasing"):
            LengthConfig(floor_words=500, ideal_min_words=200)

    def test_a_reckless_near_duplicate_threshold_is_rejected(self) -> None:
        from pulpmill.config.models import NearDuplicateConfig

        # Past ~10 bits, distinct stories start merging; the bound is hard.
        with pytest.raises(ValueError, match="less than or equal to 12"):
            NearDuplicateConfig(hamming_threshold=13, band_count=4)

    def test_the_near_duplicate_threshold_stays_conservative(self) -> None:
        """Regression: a threshold of 6 merged two unrelated nosleep stories.

        Measured over real ingested content, the closest pair of genuinely
        different same-genre stories sits at Hamming distance 5. Same-genre
        long-form prose converges in SimHash space, so the usable margin is far
        tighter than a synthetic "same story, one word changed" pair suggests.
        Anything above 4 starts merging distinct stories.
        """
        from pulpmill.config.models import NearDuplicateConfig

        assert NearDuplicateConfig().hamming_threshold <= 4

    def test_the_shipped_threshold_keeps_recall_guaranteed(self, project_root: Path) -> None:
        """The default must stay below band_count so LSH recall is provable."""
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert config.deduplication.layers.near_duplicate.recall_is_guaranteed

    def test_recall_guarantee_is_reported_honestly(self) -> None:
        """Below band_count the index provably finds every match; above it not."""
        from pulpmill.config.models import NearDuplicateConfig

        assert NearDuplicateConfig(hamming_threshold=3, band_count=4).recall_is_guaranteed
        assert not NearDuplicateConfig(hamming_threshold=6, band_count=4).recall_is_guaranteed


class TestSourceConfig:
    def test_quality_overrides_are_looked_up_by_key(self, project_root: Path) -> None:
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert config.source_quality("reddit", "AmItheAsshole") == 0.95
        assert config.source_quality("reddit", "unlisted_sub") == config.sources["reddit"].quality
        assert config.source_quality("reddit", None) == config.sources["reddit"].quality

    def test_unknown_platform_gets_a_neutral_quality(self, project_root: Path) -> None:
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert config.source_quality("not_a_platform", "x") == 0.5

    def test_platform_without_a_metric_reports_no_reference(self, project_root: Path) -> None:
        """4chan has no score, so its score reference is explicitly null."""
        config = load_config(project_root=project_root, environ={}, load_dotenv=False)
        assert config.engagement_references("fourchan").score_reference is None
        assert config.engagement_references("fourchan").comment_reference is not None


class TestSecrets:
    def test_env_file_parsing(self) -> None:
        parsed = parse_env_file(
            "\n".join(
                [
                    "# a comment",
                    "",
                    "PLAIN=value",
                    "export EXPORTED=value2",
                    'QUOTED="quoted value"',
                    "SINGLE='single'",
                    "WITH_EQUALS=a=b=c",
                    "EMPTY=",
                    "no_equals_line",
                ]
            )
        )
        assert parsed == {
            "PLAIN": "value",
            "EXPORTED": "value2",
            "QUOTED": "quoted value",
            "SINGLE": "single",
            "WITH_EQUALS": "a=b=c",
            "EMPTY": "",
        }

    def test_env_file_does_not_overwrite_real_environment(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("KEY=from_file\nOTHER=from_file")
        environ = {"KEY": "from_real_env"}
        applied = load_env_file(path, environ)
        assert environ["KEY"] == "from_real_env"
        assert environ["OTHER"] == "from_file"
        assert applied == 1

    def test_missing_env_file_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_env_file(tmp_path / "absent", {}) == 0

    def test_blank_values_read_as_unset(self) -> None:
        """A `.env` copied from `.env.example` is full of blanks."""
        store = SecretStore(environ={"PULPMILL_REDDIT_CLIENT_ID": "   "})
        assert store.get("REDDIT_CLIENT_ID") is None
        assert store.has("REDDIT_CLIENT_ID") is False

    def test_unprefixed_lookup_for_third_party_variables(self) -> None:
        store = SecretStore(environ={"ANTHROPIC_API_KEY": "sk-test"})
        assert store.get("ANTHROPIC_API_KEY", prefixed=False) == "sk-test"
        assert store.get("ANTHROPIC_API_KEY") is None

    def test_repr_never_exposes_values(self) -> None:
        store = SecretStore(environ={"PULPMILL_REDDIT_CLIENT_SECRET": "super-secret"})
        assert "super-secret" not in repr(store)

    def test_require_raises_for_a_missing_secret(self) -> None:
        with pytest.raises(KeyError, match="PULPMILL_NOPE"):
            SecretStore(environ={}).require("NOPE")
