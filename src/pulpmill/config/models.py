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


class RateLimitConfig(StrictModel):
    requests_per_second: PositiveFloat = 1.0
    burst: PositiveInt = 1


# --- production ---------------------------------------------------------------


class ClaudeScriptConfig(StrictModel):
    model: str = "claude-opus-5"
    max_output_tokens: PositiveInt = 4000
    timeout_seconds: PositiveFloat = 60.0
    max_attempts: PositiveInt = 2


class ScriptConfig(StrictModel):
    """How a story becomes a narration script.

    Durations are the contract with the rest of production: segmentation aims
    for `target_seconds` per part and never exceeds `max_seconds`, which is what
    keeps a rendered video inside the platform ceilings enforced later by
    `validation`.
    """

    provider: Literal["deterministic", "claude"] = "deterministic"
    #: Bump when script-building behaviour changes. Stored on every script.
    version: str = "2026.08.1"
    #: Narration rate used to convert words to seconds before any audio exists.
    #: Must stay close to the TTS voice's real rate or parts drift long.
    words_per_minute: PositiveFloat = 150.0
    target_seconds: PositiveFloat = 55.0
    min_seconds: PositiveFloat = 15.0
    max_seconds: PositiveFloat = 170.0
    #: A story needing more parts than this is rejected rather than split into
    #: a series nobody will finish watching.
    max_parts: PositiveInt = 6
    include_hook: bool = True
    include_outro: bool = True
    #: `{next_part}` and `{total_parts}` are substituted. Used on every part
    #: except the last.
    outro_template: str = "Part {next_part} is up next."
    final_outro: str = "Follow for more stories like this."
    claude: ClaudeScriptConfig = ClaudeScriptConfig()

    @model_validator(mode="after")
    def _durations_ordered(self) -> ScriptConfig:
        if not self.min_seconds < self.target_seconds <= self.max_seconds:
            raise ValueError(
                "script durations must satisfy min_seconds < target_seconds <= max_seconds"
            )
        return self


class KokoroConfig(StrictModel):
    """Kokoro-82M specifics.

    `repo_id` and `model_path` are alternatives: leave `model_path` empty to let
    the package resolve its own weights, or point it at a local ONNX file for a
    fully offline install.
    """

    repo_id: str = "hexgrad/Kokoro-82M"
    model_path: str = ""
    voices_path: str = ""
    #: Kokoro is a 82M-parameter model; it fits comfortably on a 6 GB card, but
    #: CPU synthesis is viable and is the safe default on a shared machine.
    device: Literal["auto", "cuda", "cpu"] = "auto"


class TTSConfig(StrictModel):
    provider: Literal["mock", "kokoro"] = "kokoro"
    voice: str = "af_heart"
    speed: PositiveFloat = 1.0
    #: espeak-ng language code. Must include a region.
    language: str = "en-us"
    sample_rate: PositiveInt = 24000
    #: Synthesised clips are cached by a key covering text, voice, speed and
    #: model version, so re-rendering a video costs no synthesis.
    cache_dir: str = "var/audio"
    #: Silence inserted between sentences, which is also what makes sentence
    #: boundaries audible enough for captions to feel aligned.
    kokoro: KokoroConfig = KokoroConfig()
    sentence_gap_seconds: NonNegativeFloat = 0.28
    paragraph_gap_seconds: NonNegativeFloat = 0.45
    #: Fail a synthesis whose measured duration exceeds this. Guards against a
    #: model looping on malformed input and producing a 20-minute clip.
    max_clip_seconds: PositiveFloat = 300.0


class CaptionConfig(StrictModel):
    """Burned-in caption styling and cue grouping.

    Colours are ASS `&HAABBGGRR` strings -- alpha first, then blue, green, red.
    That byte order is the format's, not a typo.
    """

    enabled: bool = True
    font_family: str = "DejaVu Sans"
    font_size: PositiveInt = 72
    bold: bool = True
    primary_colour: str = "&H00FFFFFF"
    highlight_colour: str = "&H0000D7FF"
    outline_colour: str = "&H00000000"
    outline_width: NonNegativeFloat = 5.0
    shadow_depth: NonNegativeFloat = 2.0
    #: Word-by-word highlighting as the narrator speaks. Requires word timings;
    #: falls back to whole-cue display when the provider cannot supply them.
    karaoke: bool = True
    max_words_per_cue: PositiveInt = 4
    #: Sized against the usable width: 1080px less two 8% margins is ~908px,
    #: and bold DejaVu Sans averages ~0.55em per character, so 22 characters at
    #: 72px is about 870px. Raising either this or `font_size` without
    #: recomputing that produces cues that wrap to two lines.
    max_chars_per_cue: PositiveInt = 22
    min_cue_seconds: PositiveFloat = 0.4
    #: Vertical placement as a fraction of frame height, measured from the top.
    vertical_position: UnitFloat = 0.62
    #: Horizontal margin as a fraction of frame width, kept clear of platform
    #: UI overlays on both sides.
    horizontal_margin: UnitFloat = 0.08


class ProceduralBackgroundConfig(StrictModel):
    """The generated background used until real footage is available.

    Not a placeholder: it renders a real, deterministic animated gradient, so
    the whole pipeline is runnable and testable before any asset exists.
    """

    #: `#rrggbb`. Two colours define the gradient; the seed picks a rotation.
    top_colour: str = "#141726"
    bottom_colour: str = "#2b1035"
    #: Subtle film grain, which stops large flat gradients from banding.
    grain: UnitFloat = 0.06


class BackgroundConfig(StrictModel):
    """Where the moving background behind the captions comes from.

    `auto` uses the clip library when it contains usable footage and the
    procedural generator when it does not, which is what lets the pipeline run
    end to end today and switch over the moment footage is dropped in.
    """

    mode: Literal["auto", "library", "procedural"] = "auto"
    library_dir: str = "assets/backgrounds"
    #: Extensions scanned in the library directory.
    extensions: tuple[str, ...] = (".mp4", ".mov", ".mkv", ".webm")
    #: Clips shorter than this cannot fill a part without an obvious loop.
    min_clip_seconds: PositiveFloat = 20.0
    #: Start the clip at a deterministic offset derived from the story id, so
    #: consecutive videos using the same footage do not open on the same frame.
    randomise_start: bool = True
    procedural: ProceduralBackgroundConfig = ProceduralBackgroundConfig()


class WatermarkConfig(StrictModel):
    #: Off until an asset exists. Enabling it without `path` present is a
    #: configuration error raised at render time, not a silently skipped
    #: overlay.
    enabled: bool = False
    path: str = "assets/branding/watermark.png"
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    #: Width as a fraction of frame width.
    scale: UnitFloat = 0.18
    opacity: UnitFloat = 0.75
    margin_ratio: UnitFloat = 0.04


class TitleCardConfig(StrictModel):
    """The opening title, drawn over the background for the first few seconds."""

    enabled: bool = True
    seconds: PositiveFloat = 2.6
    font_size: PositiveInt = 58
    max_chars: PositiveInt = 90
    #: Fraction of frame height, measured from the top. Kept well clear of the
    #: caption band so the two never collide on a long title.
    vertical_position: UnitFloat = 0.30


class RenderConfig(StrictModel):
    width: PositiveInt = 1080
    height: PositiveInt = 1920
    fps: PositiveInt = 30
    #: `auto` probes ffmpeg once and prefers NVENC when the build has it.
    encoder: Literal["auto", "h264_nvenc", "libx264"] = "auto"
    #: Quality level. Interpreted as -cq for NVENC and -crf for libx264; the
    #: scales are close enough that one number serves both.
    quality: Annotated[int, Field(ge=0, le=51)] = 23
    preset: str = "p5"
    libx264_preset: str = "medium"
    #: Bitrate ceiling. Constant-quality alone produces 6+ Mbps on animated
    #: gradients with grain, which is far past the point of visible improvement
    #: and makes every upload slower. Empty disables the cap.
    max_bitrate: str = "5M"
    audio_bitrate: str = "192k"
    #: EBU R128 target. -14 LUFS is what the major platforms normalise to, so
    #: hitting it here avoids them turning the audio down unpredictably.
    loudness_lufs: float = -14.0
    output_dir: str = "var/video"
    #: Hard ceiling on one ffmpeg invocation. A render that exceeds it is
    #: killed rather than left to occupy the GPU indefinitely.
    timeout_seconds: PositiveFloat = 900.0
    background: BackgroundConfig = BackgroundConfig()
    watermark: WatermarkConfig = WatermarkConfig()
    title_card: TitleCardConfig = TitleCardConfig()

    @model_validator(mode="after")
    def _vertical_frame(self) -> RenderConfig:
        if self.height <= self.width:
            raise ValueError("render dimensions must be portrait (height > width)")
        if self.width % 2 or self.height % 2:
            raise ValueError("render dimensions must be even for yuv420p encoding")
        return self


class ValidationConfig(StrictModel):
    """Checks a rendered file must pass before it is publishable.

    This is the gate that stops a bad batch reaching a platform, so the defaults
    are deliberately strict rather than permissive.
    """

    min_seconds: PositiveFloat = 12.0
    #: Shorts, Reels and TikTok all accept three minutes; staying under it means
    #: one render is publishable everywhere.
    max_seconds: PositiveFloat = 179.0
    max_bytes: PositiveInt = 300 * 1024 * 1024
    require_audio: bool = True
    #: Mean volume below this means the audio track is effectively silent --
    #: usually a muxing mistake rather than a quiet voice.
    min_mean_volume_dbfs: float = -45.0
    require_expected_dimensions: bool = True
    #: Rendered duration must match the narration within this tolerance.
    duration_tolerance_seconds: PositiveFloat = 1.5


class PublishTargetConfig(StrictModel):
    """One publishing destination.

    Every target ships disabled. Enabling one requires credentials *and*, on
    every platform, an approval step that cannot be automated -- see
    docs/PUBLISHING.md.
    """

    enabled: bool = False
    adapter: str
    rate_limit: RateLimitConfig = RateLimitConfig()
    #: Start private. A public default plus a bug is an unrecoverable mistake.
    privacy: Literal["private", "unlisted", "public"] = "private"
    #: Per-day upload ceiling enforced locally, independent of the platform's.
    daily_limit: PositiveInt = 5
    title_max_chars: PositiveInt = 100
    description_max_chars: PositiveInt = 4500
    hashtags: tuple[str, ...] = ()
    options: dict[str, Any] = Field(default_factory=dict)


class PublishingConfig(StrictModel):
    #: Global safety interlock. While true, adapters validate and build the
    #: request but never transmit. Turning it off is a deliberate act.
    dry_run: bool = True
    #: Appended to every description. Attribution is not permission, but it
    #: makes a takedown a conversation rather than a strike.
    attribution_template: str = "Source: {url}"
    targets: dict[str, PublishTargetConfig] = Field(default_factory=dict)

    def enabled_targets(self) -> dict[str, PublishTargetConfig]:
        return {name: cfg for name, cfg in self.targets.items() if cfg.enabled}


# --- sources -----------------------------------------------------------------


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
    #: Communities this source must never ingest, matched against the story's
    #: `quality_key` metadata. Enforced by the core before a story is
    #: persisted, so it holds even if a blocked community is left in `queries`.
    #: Generic by design: subreddits for Reddit, boards for 4chan, one
    #: mechanism. See docs/CONTENT_POLICY.md.
    blocked_quality_keys: tuple[str, ...] = ()

    # --- adapter-owned sections; validated by the adapter, not here ---
    filters: dict[str, Any] = Field(default_factory=dict)
    queries: tuple[dict[str, Any], ...] = ()
    options: dict[str, Any] = Field(default_factory=dict)

    def quality_for(self, quality_key: str | None) -> float:
        if quality_key is None:
            return self.quality
        return self.quality_overrides.get(quality_key, self.quality)

    def is_blocked(self, quality_key: str | None) -> bool:
        """Whether policy forbids ingesting from this community.

        Case-insensitive on purpose: Reddit treats `r/NoSleep` and `r/nosleep`
        as the same subreddit, and a blocklist that a capitalisation defeats is
        not a blocklist.
        """
        if quality_key is None:
            return False
        folded = quality_key.casefold()
        return any(blocked.casefold() == folded for blocked in self.blocked_quality_keys)


# --- root --------------------------------------------------------------------


class AppConfig(StrictModel):
    version: Literal[1] = 1
    runtime: RuntimeConfig = RuntimeConfig()
    http: HttpConfig = HttpConfig()
    ingestion: IngestionConfig = IngestionConfig()
    deduplication: DeduplicationConfig = DeduplicationConfig()
    ranking: RankingConfig = RankingConfig()
    editorial: EditorialConfig = EditorialConfig()
    script: ScriptConfig = ScriptConfig()
    tts: TTSConfig = TTSConfig()
    captions: CaptionConfig = CaptionConfig()
    render: RenderConfig = RenderConfig()
    validation: ValidationConfig = ValidationConfig()
    publishing: PublishingConfig = PublishingConfig()
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

    @property
    def audio_cache_dir(self) -> Path:
        return self.resolve(self.tts.cache_dir)

    @property
    def video_output_dir(self) -> Path:
        return self.resolve(self.render.output_dir)

    @property
    def background_library_dir(self) -> Path:
        return self.resolve(self.render.background.library_dir)

    def production_fingerprint(self) -> str:
        """Stable hash of everything that changes a rendered file.

        Stored on every video so a re-render under different settings is
        detectable, and so an unchanged configuration can skip work entirely.
        """
        payload = {
            "script": self.script.model_dump(mode="json"),
            "tts": self.tts.model_dump(mode="json"),
            "captions": self.captions.model_dump(mode="json"),
            "render": self.render.model_dump(mode="json"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

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
