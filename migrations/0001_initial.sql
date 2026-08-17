-- pulpmill initial schema.
--
-- Conventions used throughout:
--   * Timestamps are ISO-8601 UTC strings ending in 'Z'. They sort
--     lexicographically, which lets SQLite use plain B-tree indexes for
--     time-ordered queries without a date type.
--   * Structured payloads are stored as JSON text columns suffixed `_json`.
--   * Every natural key that must be idempotent has a UNIQUE constraint, so
--     re-running any stage is a no-op rather than a duplicate insert.

-- ---------------------------------------------------------------------------
-- stories: the canonical normalized story record.
-- ---------------------------------------------------------------------------
-- `id` is a deterministic UUIDv5 of (source_platform, source_id), so the same
-- upstream post always maps to the same primary key across machines and runs.
--
-- Provenance columns (source_platform, source_id, canonical_url, author, title)
-- are NOT NULL where the source can guarantee them and are never rewritten by
-- later stages. Everything downstream -- scripts, audio, video -- must be able
-- to walk back to the originating URL.
CREATE TABLE stories (
    id                 TEXT    NOT NULL PRIMARY KEY,
    source_platform    TEXT    NOT NULL,
    source_id          TEXT    NOT NULL,
    canonical_url      TEXT    NOT NULL,
    -- SHA-256 of the normalized canonical URL. Dedup layer 2 matches on this
    -- rather than the raw URL so tracking params and host variants collapse.
    url_fingerprint    TEXT    NOT NULL,
    author             TEXT,
    title              TEXT    NOT NULL,
    raw_content        TEXT    NOT NULL,
    normalized_content TEXT    NOT NULL,
    -- SHA-256 of the normalized content. Dedup layer 3.
    content_hash       TEXT    NOT NULL,
    -- 64-bit SimHash as a zero-padded 16-char hex string. NULL when the body
    -- was too short to fingerprint stably. Dedup layer 4.
    simhash            TEXT,
    word_count         INTEGER NOT NULL,
    language           TEXT,
    created_at         TEXT    NOT NULL,
    discovered_at      TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    engagement_json    TEXT    NOT NULL,
    metadata_json      TEXT    NOT NULL,
    status             TEXT    NOT NULL,
    -- Set when this story was identified as a duplicate of an earlier one.
    duplicate_of_id    TEXT             REFERENCES stories (id) ON DELETE SET NULL,
    duplicate_layer    TEXT,

    CONSTRAINT stories_source_unique UNIQUE (source_platform, source_id),
    CONSTRAINT stories_status_valid CHECK (status IN (
        'DISCOVERED',
        'NORMALIZED',
        'DEDUPLICATED',
        'DUPLICATE',
        'REJECTED',
        'RANKED',
        'SELECTED',
        'SCRIPT_PENDING',
        'SCRIPT_READY',
        'AUDIO_PENDING',
        'AUDIO_READY',
        'VIDEO_PENDING',
        'VIDEO_READY',
        'VALIDATED',
        'PUBLISHED',
        'FAILED'
    )),
    CONSTRAINT stories_word_count_valid CHECK (word_count >= 0),
    CONSTRAINT stories_not_self_duplicate CHECK (duplicate_of_id IS NULL OR duplicate_of_id <> id)
);

CREATE INDEX stories_url_fingerprint_idx ON stories (url_fingerprint);
CREATE INDEX stories_content_hash_idx    ON stories (content_hash);
CREATE INDEX stories_status_idx          ON stories (status);
CREATE INDEX stories_created_at_idx      ON stories (created_at);
CREATE INDEX stories_discovered_at_idx   ON stories (discovered_at);
CREATE INDEX stories_platform_status_idx ON stories (source_platform, status);
CREATE INDEX stories_duplicate_of_idx    ON stories (duplicate_of_id);

-- ---------------------------------------------------------------------------
-- story_simhash_bands: banded LSH index for dedup layer 4.
-- ---------------------------------------------------------------------------
-- A 64-bit SimHash is split into `band_count` equal slices. Two fingerprints
-- within a small Hamming distance almost always share at least one identical
-- band, so an indexed equality lookup replaces a full-table scan.
CREATE TABLE story_simhash_bands (
    story_id   TEXT    NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    band_index INTEGER NOT NULL,
    band_value TEXT    NOT NULL,

    PRIMARY KEY (story_id, band_index)
) WITHOUT ROWID;

CREATE INDEX story_simhash_bands_lookup_idx ON story_simhash_bands (band_index, band_value);

-- ---------------------------------------------------------------------------
-- story_state_events: append-only audit of every state transition.
-- ---------------------------------------------------------------------------
-- Lets us answer "why is this story in this state" and reconstruct pipeline
-- history after a crash without trusting in-memory state.
CREATE TABLE story_state_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id    TEXT NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    stage       TEXT NOT NULL,
    job_id      TEXT,
    reason      TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX story_state_events_story_idx ON story_state_events (story_id, id);
CREATE INDEX story_state_events_job_idx   ON story_state_events (job_id);

-- ---------------------------------------------------------------------------
-- story_rankings: one row per (story, ranking version, config fingerprint).
-- ---------------------------------------------------------------------------
-- The UNIQUE constraint is what makes ranking idempotent: re-ranking with the
-- same version and the same weights cannot create a second row.
--
-- `reference_time` is stored because recency scoring depends on "now"; keeping
-- it makes a past ranking exactly reproducible.
CREATE TABLE story_rankings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id              TEXT NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    ranking_version       TEXT NOT NULL,
    config_fingerprint    TEXT NOT NULL,
    final_score           REAL NOT NULL,
    component_scores_json TEXT NOT NULL,
    weights_json          TEXT NOT NULL,
    explanation_json      TEXT NOT NULL,
    reference_time        TEXT NOT NULL,
    ranked_at             TEXT NOT NULL,

    CONSTRAINT story_rankings_unique UNIQUE (story_id, ranking_version, config_fingerprint)
);

CREATE INDEX story_rankings_score_idx   ON story_rankings (final_score DESC);
CREATE INDEX story_rankings_version_idx ON story_rankings (ranking_version, config_fingerprint, final_score DESC);

-- ---------------------------------------------------------------------------
-- jobs / job_failures: observability for long-running workers.
-- ---------------------------------------------------------------------------
CREATE TABLE jobs (
    id          TEXT NOT NULL PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    params_json TEXT NOT NULL,
    stats_json  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    error       TEXT,

    CONSTRAINT jobs_status_valid CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'INTERRUPTED'))
);

CREATE INDEX jobs_started_at_idx ON jobs (started_at DESC);
CREATE INDEX jobs_status_idx     ON jobs (status);

-- Failures are persisted rather than only logged, so a 24/7 worker can be
-- audited after the fact and retried selectively.
CREATE TABLE job_failures (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT REFERENCES jobs (id) ON DELETE CASCADE,
    story_id        TEXT,
    source_platform TEXT,
    stage           TEXT NOT NULL,
    operation       TEXT NOT NULL,
    error_type      TEXT NOT NULL,
    error_message   TEXT NOT NULL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    context_json    TEXT NOT NULL DEFAULT '{}',
    occurred_at     TEXT NOT NULL
);

CREATE INDEX job_failures_job_idx      ON job_failures (job_id);
CREATE INDEX job_failures_occurred_idx ON job_failures (occurred_at DESC);
CREATE INDEX job_failures_stage_idx    ON job_failures (stage);

-- ---------------------------------------------------------------------------
-- editorial_batches / editorial_selections: the selection stage.
-- ---------------------------------------------------------------------------
-- A batch records which provider produced an ordering and whether it fell back
-- to deterministic order. Selections are the ordered output.
CREATE TABLE editorial_batches (
    id                 TEXT NOT NULL PRIMARY KEY,
    provider           TEXT NOT NULL,
    effective_provider TEXT NOT NULL,
    fallback_reason    TEXT,
    ranking_version    TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    candidate_count    INTEGER NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE INDEX editorial_batches_created_idx ON editorial_batches (created_at DESC);

CREATE TABLE editorial_selections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT    NOT NULL REFERENCES editorial_batches (id) ON DELETE CASCADE,
    story_id      TEXT    NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    rationale     TEXT,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,

    CONSTRAINT editorial_selections_unique_story    UNIQUE (batch_id, story_id),
    CONSTRAINT editorial_selections_unique_position UNIQUE (batch_id, position),
    CONSTRAINT editorial_selections_position_valid  CHECK (position >= 1)
);

CREATE INDEX editorial_selections_story_idx ON editorial_selections (story_id);

-- ---------------------------------------------------------------------------
-- story_series / story_parts: multi-part video support.
-- ---------------------------------------------------------------------------
-- Not exercised by tonight's ingestion slice, but the schema exists now so the
-- rendering stages can be added without a migration that rewrites stories.
--
-- part_number/total_parts are computed by the pipeline. No model is permitted
-- to invent them; the CHECK constraints below enforce that they stay coherent.
CREATE TABLE story_series (
    id            TEXT    NOT NULL PRIMARY KEY,
    story_id      TEXT    NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    total_parts   INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,

    CONSTRAINT story_series_total_parts_valid CHECK (total_parts >= 1)
);

CREATE INDEX story_series_story_idx ON story_series (story_id);

CREATE TABLE story_parts (
    id            TEXT    NOT NULL PRIMARY KEY,
    series_id     TEXT    NOT NULL REFERENCES story_series (id) ON DELETE CASCADE,
    story_id      TEXT    NOT NULL REFERENCES stories (id) ON DELETE CASCADE,
    part_number   INTEGER NOT NULL,
    total_parts   INTEGER NOT NULL,
    -- Character offsets into stories.normalized_content, so a part always
    -- resolves back to the exact source text it was cut from.
    content_start INTEGER NOT NULL,
    content_end   INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,

    CONSTRAINT story_parts_unique_number  UNIQUE (series_id, part_number),
    CONSTRAINT story_parts_number_valid   CHECK (part_number >= 1 AND part_number <= total_parts),
    CONSTRAINT story_parts_offsets_valid  CHECK (content_start >= 0 AND content_end > content_start)
);

CREATE INDEX story_parts_series_idx ON story_parts (series_id, part_number);
CREATE INDEX story_parts_story_idx  ON story_parts (story_id);
