"""Validated configuration models.

Every model forbids unknown keys, so a typo in `pipeline.yaml` fails loudly at
startup instead of silently reverting to a default six hours into a run.

One deliberate exception: `SourceConfig.filters`, `.queries` and `.options` are
free-form mappings. Those are the *adapter-specific* parts of the config, and
each adapter validates its own slice with its own model. That is what keeps
"add a source" from meaning "edit the core config model".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]
PositiveFloat = Annotated[float, Field(gt=0.0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class StrictModel(BaseModel):
    """Base for every config model: immutable and intolerant of typos."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --- runtime -----------------------------------------------------------------


class DatabaseConfig(StrictModel):
    path: str = "var/pulpmill.db"
    busy_timeout_ms: PositiveInt = 5000
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY"] = "WAL"
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"
    foreign_keys: bool = True


class LogFileConfig(StrictModel):
    enabled: bool = True
    path: str = "var/logs/pulpmill.jsonl"
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    max_bytes: PositiveInt = 10 * 1024 * 1024
    backup_count: NonNegativeInt = 5


class LoggingConfig(StrictModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console_format: Literal["pretty", "json"] = "pretty"
    file: LogFileConfig = LogFileConfig()


class RuntimeConfig(StrictModel):
    data_dir: str = "var"
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()


# --- http --------------------------------------------------------------------


class TimeoutConfig(StrictModel):
    connect_seconds: PositiveFloat = 5.0
    read_seconds: PositiveFloat = 20.0
    write_seconds: PositiveFloat = 10.0
    pool_seconds: PositiveFloat = 5.0


class PoolConfig(StrictModel):
    max_connections: PositiveInt = 8
    max_keepalive_connections: PositiveInt = 4
    keepalive_expiry_seconds: PositiveFloat = 30.0

    @model_validator(mode="after")
    def _keepalive_within_total(self) -> PoolConfig:
        if self.max_keepalive_connections > self.max_connections:
            raise ValueError("max_keepalive_connections cannot exceed max_connections")
        return self


class RetryConfig(StrictModel):
    max_attempts: PositiveInt = 4
    initial_backoff_seconds: PositiveFloat = 1.0
    max_backoff_seconds: PositiveFloat = 60.0
    multiplier: Annotated[float, Field(ge=1.0)] = 2.0
    #: Fraction of the computed delay used as random jitter, to avoid every
    #: worker retrying in lockstep. Set to 0 for fully deterministic backoff.
    jitter_ratio: UnitFloat = 0.2
    retry_on_status: tuple[int, ...] = (408, 429, 500, 502, 503, 504)
    respect_retry_after: bool = True
    #: A Retry-After longer than this is treated as a hard failure rather than
    #: blocking a worker.
    max_retry_after_seconds: PositiveFloat = 120.0

    @model_validator(mode="after")
    def _backoff_ordering(self) -> RetryConfig:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        return self


class HttpConfig(StrictModel):
    user_agent: str = "pulpmill/0.1.0 (local story pipeline)"
    timeout: TimeoutConfig = TimeoutConfig()
    pool: PoolConfig = PoolConfig()
    retry: RetryConfig = RetryConfig()


# --- ingestion ---------------------------------------------------------------


class IngestionConfig(StrictModel):
    max_stories_per_source: PositiveInt = 120
    max_pages_per_query: PositiveInt = 4
    stop_on_exhausted_page: bool = True


# --- deduplication -----------------------------------------------------------


class NearDuplicateConfig(StrictModel):
    enabled: bool = True
    algorithm: Literal["simhash64"] = "simhash64"
    #: Maximum Hamming distance between two 64-bit fingerprints to call a match.
    #:
    #: Calibrated against real ingested stories, not synthetic pairs. Measured
    #: over r/nosleep and /x/ content: the closest pair of *genuinely different*
    #: stories sits at distance 5, and unrelated pairs have a median of 15.
    #: Same-genre long-form prose converges in SimHash space -- two first-person
    #: horror stories share so much vocabulary that the distinguishing signal
    #: washes out -- so the usable margin is much tighter than a synthetic
    #: "same story, one word changed" test suggests.
    #:
    #: 3 produced zero false positives on that corpus, leaves a 2-bit margin,
    #: and keeps recall provably complete (3 < band_count). A previous value of
    #: 6 merged two unrelated nosleep stories on the first live run.
    #: Do not raise this without re-measuring against real data.
    hamming_threshold: Annotated[int, Field(ge=0, le=12)] = 3
    min_tokens: PositiveInt = 40
    #: Bands the fingerprint is split into for the LSH index lookup. Fewer,
    #: wider bands are more selective (fewer candidates examined per query);
    #: more, narrower bands trade that for recall.
    band_count: Literal[2, 4, 8] = 4

    @property
    def recall_is_guaranteed(self) -> bool:
        """Whether the band index provably finds every match at this threshold.

        Pigeonhole: two fingerprints differing in fewer than `band_count` bits
        must share at least one identical band. Above that point the index still
        finds most matches -- differing bits rarely spread evenly across bands --
        but no longer promises to. Best-effort recall is acceptable for a
        supplementary layer sitting behind three exact ones, which is why this
        is reported rather than enforced.
        """
        return self.hamming_threshold < self.band_count


class DedupLayersConfig(StrictModel):
    exact_source: bool = True
    canonical_url: bool = True
    content_hash: bool = True
    near_duplicate: NearDuplicateConfig = NearDuplicateConfig()


class DeduplicationConfig(StrictModel):
    layers: DedupLayersConfig = DedupLayersConfig()


# --- ranking -----------------------------------------------------------------


class RankingWeights(StrictModel):
    engagement: NonNegativeFloat = 0.22
    recency: NonNegativeFloat = 0.12
    comment_activity: NonNegativeFloat = 0.08
    narrative_suitability: NonNegativeFloat = 0.26
    length: NonNegativeFloat = 0.12
    novelty: NonNegativeFloat = 0.12
    source_quality: NonNegativeFloat = 0.08

    @model_validator(mode="after")
    def _at_least_one_positive(self) -> RankingWeights:
        if sum(self.as_mapping().values()) <= 0:
            raise ValueError("at least one ranking weight must be greater than zero")
        return self

    def as_mapping(self) -> dict[str, float]:
        return self.model_dump()


class RecencyConfig(StrictModel):
    half_life_hours: PositiveFloat = 36.0
    max_age_hours: PositiveFloat = 336.0

    @model_validator(mode="after")
    def _max_age_exceeds_half_life(self) -> RecencyConfig:
        if self.max_age_hours <= self.half_life_hours:
            raise ValueError("max_age_hours must be greater than half_life_hours")
        return self


class LengthConfig(StrictModel):
    """Trapezoid response over word count of the narratable body."""

    floor_words: PositiveInt = 120
    ideal_min_words: PositiveInt = 320
    ideal_max_words: PositiveInt = 900
    ceiling_words: PositiveInt = 2200

    @model_validator(mode="after")
    def _monotonic(self) -> LengthConfig:
        bounds = (
            self.floor_words,
            self.ideal_min_words,
            self.ideal_max_words,
            self.ceiling_words,
        )
        if list(bounds) != sorted(bounds) or len(set(bounds)) != len(bounds):
            raise ValueError(
                "length bounds must be strictly increasing: "
                "floor_words < ideal_min_words < ideal_max_words < ceiling_words"
            )
        return self


class CommentActivityConfig(StrictModel):
    reference_comments_per_hour: PositiveFloat = 25.0
    min_age_hours: NonNegativeFloat = 1.0


class NoveltyConfig(StrictModel):
    lookback_stories: PositiveInt = 400
    shingle_size: Annotated[int, Field(ge=1, le=8)] = 3
    min_tokens: PositiveInt = 20
    #: Only the title plus this many characters of the body are compared. Keeps
    #: the lookback corpus bounded in memory (this runs 24/7 on 16 GB) while
    #: still catching "we already have this story".
    compare_chars: PositiveInt = 1200


class NarrativeSuitabilityConfig(StrictModel):
    first_person_weight: UnitFloat = 0.22
    dialogue_weight: UnitFloat = 0.14
    conflict_weight: UnitFloat = 0.20
    temporal_structure_weight: UnitFloat = 0.16
    paragraph_structure_weight: UnitFloat = 0.12
    title_hook_weight: UnitFloat = 0.16
    link_heavy_penalty: UnitFloat = 0.35
    shouting_penalty: UnitFloat = 0.15
    meta_post_penalty: UnitFloat = 0.40


class RankingConfig(StrictModel):
    version: str = "2026.08.1"
    weights: RankingWeights = RankingWeights()
    recency: RecencyConfig = RecencyConfig()
    length: LengthConfig = LengthConfig()
    comment_activity: CommentActivityConfig = CommentActivityConfig()
    novelty: NoveltyConfig = NoveltyConfig()
    narrative_suitability: NarrativeSuitabilityConfig = NarrativeSuitabilityConfig()

    def fingerprint(self) -> str:
        """Stable hash of everything that affects a score.

        Stored on every ranking row. Change a weight and the fingerprint
        changes, so old scores stay attributable to the config that produced
        them instead of being silently overwritten.
        """
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


# --- editorial ---------------------------------------------------------------


class ClaudeEditorialConfig(StrictModel):
    model: str = "claude-opus-5"
    #: Caps thinking *and* response text together on current models, so this
    #: needs headroom well beyond the size of the JSON we expect back.
    max_output_tokens: PositiveInt = 8000
    timeout_seconds: PositiveFloat = 60.0
    max_attempts: PositiveInt = 2
    recent_selection_hours: PositiveFloat = 72.0


class EditorialConfig(StrictModel):
    provider: Literal["deterministic", "claude"] = "deterministic"
    candidate_pool_size: PositiveInt = 10
    select_count: PositiveInt = 5
    claude: ClaudeEditorialConfig = ClaudeEditorialConfig()

    @model_validator(mode="after")
    def _selection_fits_pool(self) -> EditorialConfig:
        if self.select_count > self.candidate_pool_size:
            raise ValueError("select_count cannot exceed candidate_pool_size")
        return self


# --- sources -----------------------------------------------------------------


class RateLimitConfig(StrictModel):
    requests_per_second: PositiveFloat = 1.0
    burst: PositiveInt = 1


class EngagementReferences(StrictModel):
    """Per-platform normalisation anchors.

    A story sitting exactly at the reference value scores ~0.5 on that axis.
    These are per-source because 4 000 Reddit upvotes and 80 4chan replies are
    comparable amounts of attention expressed in incomparable units.

    `None` means the platform does not report that metric at all; the engagement
    signal drops the axis rather than scoring it zero.
    """

    score_reference: PositiveFloat | None = None
    comment_reference: PositiveFloat | None = None


class SourceConfig(StrictModel):
    enabled: bool = True
    #: Registry key of the adapter implementation.
    adapter: str
    rate_limit: RateLimitConfig = RateLimitConfig()
    #: Baseline source quality in [0, 1] used by the source_quality signal.
    quality: UnitFloat = 0.5
    #: Finer-grained quality keyed by whatever the adapter records as the
    #: story's `quality_key` metadata (subreddit for Reddit, board for 4chan).
    #: Generic on purpose: the ranking signal never learns what a subreddit is.
    quality_overrides: dict[str, UnitFloat] = Field(default_factory=dict)
    engagement: EngagementReferences = EngagementReferences()

    # --- adapter-owned sections; validated by the adapter, not here ---
    filters: dict[str, Any] = Field(default_factory=dict)
    queries: tuple[dict[str, Any], ...] = ()
    options: dict[str, Any] = Field(default_factory=dict)

    def quality_for(self, quality_key: str | None) -> float:
        if quality_key is None:
            return self.quality
        return self.quality_overrides.get(quality_key, self.quality)


# --- root --------------------------------------------------------------------


class AppConfig(StrictModel):
    version: Literal[1] = 1
    runtime: RuntimeConfig = RuntimeConfig()
    http: HttpConfig = HttpConfig()
    ingestion: IngestionConfig = IngestionConfig()
    deduplication: DeduplicationConfig = DeduplicationConfig()
    ranking: RankingConfig = RankingConfig()
    editorial: EditorialConfig = EditorialConfig()
    sources: dict[str, SourceConfig] = Field(default_factory=dict)

    #: Filled in by the loader, never read from YAML. Every relative path in the
    #: config resolves against it, so nothing is tied to a machine-specific
    #: absolute path.
    project_root: Path = Field(default_factory=Path.cwd, exclude=True)

    def resolve(self, relative: str) -> Path:
        path = Path(relative).expanduser()
        return path if path.is_absolute() else (self.project_root / path)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.runtime.data_dir)

    @property
    def database_path(self) -> Path:
        return self.resolve(self.runtime.database.path)

    @property
    def log_file_path(self) -> Path:
        return self.resolve(self.runtime.logging.file.path)

    def enabled_sources(self) -> dict[str, SourceConfig]:
        return {name: cfg for name, cfg in self.sources.items() if cfg.enabled}

    def source(self, name: str) -> SourceConfig | None:
        return self.sources.get(name)

    def source_quality(self, platform: str, quality_key: str | None) -> float:
        """Quality score for a story, defaulting safely for unknown platforms."""
        source = self.sources.get(platform)
        if source is None:
            return 0.5
        return source.quality_for(quality_key)

    def engagement_references(self, platform: str) -> EngagementReferences:
        source = self.sources.get(platform)
        return source.engagement if source else EngagementReferences()


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` over `base`.

    Mappings merge key-by-key; every other type (including lists) is replaced
    outright. Replacing lists is intentional -- a local override that wants three
    subreddits should get three, not three appended to the defaults.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
