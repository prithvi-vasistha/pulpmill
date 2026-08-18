-- Production schema: script, audio, video, validation and publishing.
--
-- Follows the conventions established in 0001: ISO-8601 UTC timestamps as TEXT,
-- structured payloads in `_json` columns, and a UNIQUE constraint on every
-- natural key that has to be idempotent.
--
-- The chain of custody is enforced structurally rather than by convention.
-- Every table below carries `story_id` alongside its immediate parent, so a
-- published video can be traced to its source URL with one join instead of
-- four, and so an orphaned row is impossible to create.

-- ---------------------------------------------------------------------------
-- story_scripts: narration text, one row per publishable video.
-- ---------------------------------------------------------------------------
-- UNIQUE (story_id, part_number) is what makes re-running `script` an update
-- rather than an accumulation. `config_fingerprint` records the script/tts/
-- caption/render settings in force, so a script produced under old settings is
-- detectable without comparing its text.
--
-- part_number and total_parts are copied from story_parts deliberately. They
-- are computed by the pipeline, and duplicating them here means a script can be
-- validated in isolation -- the CHECK below cannot be satisfied by a model
-- inventing a part number after the fact.
CREATE TABLE story_scripts (
    id                 TEXT    NOT NULL PRIMARY KEY,
    story_id           TEXT    NOT NULL REFERENCES stories (id)      ON DELETE CASCADE,
    series_id          TEXT             REFERENCES story_series (id) ON DELETE SET NULL,
    part_id            TEXT             REFERENCES story_parts (id)  ON DELETE SET NULL,
    part_number        INTEGER NOT NULL,
    total_parts        INTEGER NOT NULL,
    title              TEXT    NOT NULL,
    -- Ordered narration lines: index, role, text, speech_text, paragraph_break.
    lines_json         TEXT    NOT NULL,
    word_count         INTEGER NOT NULL,
    estimated_seconds  REAL    NOT NULL,
    -- Which provider actually produced this, which one was configured, and why
    -- they differ. Same pattern as editorial_batches: a degraded run is visible
    -- in the data, not only in a log line that has since rotated away.
    generator          TEXT    NOT NULL,
    requested_provider TEXT    NOT NULL,
    fallback_reason    TEXT,
    generator_version  TEXT    NOT NULL,
    config_fingerprint TEXT    NOT NULL,
    notes              TEXT,
    metadata_json      TEXT    NOT NULL DEFAULT '{}',
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,

    CONSTRAINT story_scripts_unique_part  UNIQUE (story_id, part_number),
    CONSTRAINT story_scripts_number_valid CHECK (part_number >= 1 AND part_number <= total_parts),
    CONSTRAINT story_scripts_length_valid CHECK (word_count > 0 AND estimated_seconds > 0)
);

CREATE INDEX story_scripts_story_idx  ON story_scripts (story_id, part_number);
CREATE INDEX story_scripts_series_idx ON story_scripts (series_id);
CREATE INDEX story_scripts_config_idx ON story_scripts (config_fingerprint);

-- ---------------------------------------------------------------------------
-- audio_artifacts: synthesised narration.
-- ---------------------------------------------------------------------------
-- One row per script. `cache_key` covers text, voice, speed and model version,
-- so an unchanged script resolves to the file already on disk instead of
-- re-synthesising -- which at 200 videos a week is the difference between
-- minutes and hours of GPU time.
--
-- `word_timings_json` may be an empty array: not every provider can align, and
-- the caption stage degrades to even distribution rather than refusing.
CREATE TABLE audio_artifacts (
    id               TEXT    NOT NULL PRIMARY KEY,
    script_id        TEXT    NOT NULL REFERENCES story_scripts (id) ON DELETE CASCADE,
    story_id         TEXT    NOT NULL REFERENCES stories (id)       ON DELETE CASCADE,
    path             TEXT    NOT NULL,
    duration_seconds REAL    NOT NULL,
    sample_rate      INTEGER NOT NULL,
    voice_id         TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    model_version    TEXT    NOT NULL,
    cache_key        TEXT    NOT NULL,
    word_timings_json TEXT   NOT NULL DEFAULT '[]',
    metadata_json    TEXT    NOT NULL DEFAULT '{}',
    created_at       TEXT    NOT NULL,

    CONSTRAINT audio_artifacts_unique_script UNIQUE (script_id),
    CONSTRAINT audio_artifacts_duration_valid CHECK (duration_seconds > 0)
);

CREATE INDEX audio_artifacts_story_idx ON audio_artifacts (story_id);
CREATE INDEX audio_artifacts_cache_idx ON audio_artifacts (cache_key);

-- ---------------------------------------------------------------------------
-- video_artifacts: the rendered file.
-- ---------------------------------------------------------------------------
-- `production_fingerprint` is the hash of every setting that affects the
-- output. Comparing it to the current configuration answers "is this file
-- stale" without re-rendering to find out.
--
-- `background_source` records which clip was used, so a batch that shares
-- footage is findable -- needed both for variety auditing and for the case
-- where a footage source turns out to be unusable after the fact.
CREATE TABLE video_artifacts (
    id                     TEXT    NOT NULL PRIMARY KEY,
    script_id              TEXT    NOT NULL REFERENCES story_scripts (id)   ON DELETE CASCADE,
    story_id               TEXT    NOT NULL REFERENCES stories (id)         ON DELETE CASCADE,
    audio_id               TEXT    NOT NULL REFERENCES audio_artifacts (id) ON DELETE CASCADE,
    path                   TEXT    NOT NULL,
    duration_seconds       REAL    NOT NULL,
    width                  INTEGER NOT NULL,
    height                 INTEGER NOT NULL,
    fps                    REAL    NOT NULL,
    size_bytes             INTEGER NOT NULL,
    encoder                TEXT    NOT NULL,
    background_source      TEXT    NOT NULL,
    production_fingerprint TEXT    NOT NULL,
    metadata_json          TEXT    NOT NULL DEFAULT '{}',
    created_at             TEXT    NOT NULL,

    CONSTRAINT video_artifacts_unique_script UNIQUE (script_id),
    CONSTRAINT video_artifacts_frame_valid   CHECK (width > 0 AND height > 0 AND fps > 0),
    CONSTRAINT video_artifacts_size_valid    CHECK (size_bytes > 0 AND duration_seconds > 0)
);

CREATE INDEX video_artifacts_story_idx  ON video_artifacts (story_id);
CREATE INDEX video_artifacts_config_idx ON video_artifacts (production_fingerprint);

-- ---------------------------------------------------------------------------
-- video_validations: the publishability gate.
-- ---------------------------------------------------------------------------
-- Append-only. Re-validating a file adds a row rather than replacing one, so a
-- file that passed under looser settings and fails under stricter ones leaves
-- both verdicts on the record.
CREATE TABLE video_validations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT    NOT NULL REFERENCES video_artifacts (id) ON DELETE CASCADE,
    story_id     TEXT    NOT NULL REFERENCES stories (id)         ON DELETE CASCADE,
    passed       INTEGER NOT NULL,
    -- Every check that ran, with its measured value -- not just the failures.
    -- "What was the loudness when it passed" is a question worth answering.
    checks_json  TEXT    NOT NULL,
    failures_json TEXT   NOT NULL DEFAULT '[]',
    validated_at TEXT    NOT NULL,

    CONSTRAINT video_validations_passed_valid CHECK (passed IN (0, 1))
);

CREATE INDEX video_validations_video_idx ON video_validations (video_id, id DESC);

-- ---------------------------------------------------------------------------
-- publications: one attempt to put one video on one platform.
-- ---------------------------------------------------------------------------
-- UNIQUE (video_id, target) makes publishing idempotent: a retry updates the
-- existing attempt rather than uploading a second copy. That constraint is the
-- only thing standing between a crash-loop and a duplicated upload.
--
-- `dry_run` is stored so a rehearsal is never mistaken for a publication.
CREATE TABLE publications (
    id            TEXT    NOT NULL PRIMARY KEY,
    video_id      TEXT    NOT NULL REFERENCES video_artifacts (id) ON DELETE CASCADE,
    script_id     TEXT    NOT NULL REFERENCES story_scripts (id)   ON DELETE CASCADE,
    story_id      TEXT    NOT NULL REFERENCES stories (id)         ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    adapter       TEXT    NOT NULL,
    state         TEXT    NOT NULL,
    dry_run       INTEGER NOT NULL DEFAULT 1,
    privacy       TEXT    NOT NULL,
    remote_id     TEXT,
    remote_url    TEXT,
    -- The metadata actually sent. Never contains credentials: adapters build
    -- their auth separately and it is not part of the request record.
    request_json  TEXT    NOT NULL DEFAULT '{}',
    response_json TEXT    NOT NULL DEFAULT '{}',
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    published_at  TEXT,

    CONSTRAINT publications_unique_target UNIQUE (video_id, target),
    CONSTRAINT publications_state_valid   CHECK (state IN (
        'PENDING',
        'UPLOADING',
        'PUBLISHED',
        'FAILED',
        'SKIPPED'
    )),
    CONSTRAINT publications_dry_run_valid CHECK (dry_run IN (0, 1))
);

CREATE INDEX publications_story_idx     ON publications (story_id);
CREATE INDEX publications_state_idx     ON publications (state);
-- Supports the per-target daily quota check, which runs before every upload.
CREATE INDEX publications_target_idx    ON publications (target, published_at DESC);
