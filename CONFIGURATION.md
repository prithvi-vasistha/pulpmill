# Configuration

Everything tunable lives in [`config/pipeline.yaml`](config/pipeline.yaml).
Secrets live in `.env`. The two never mix.

```bash
uv run pulpmill config show          # effective configuration after layering
uv run pulpmill config show --json   # same, machine-readable
uv run pulpmill config secrets       # which credentials are set (never values)
```

---

## How settings are resolved

Layered, lowest precedence first:

1. `config/pipeline.yaml` — committed defaults
2. `config/pipeline.local.yaml` — **your local overrides** (git-ignored)
3. `$PULPMILL_CONFIG` — an explicit extra file
4. A few scalar environment overrides

**Put your changes in `config/pipeline.local.yaml`.** It only needs the keys you
want to change; mappings are deep-merged.

```yaml
# config/pipeline.local.yaml
ranking:
  weights:
    narrative_suitability: 0.35
sources:
  fourchan:
    queries:
      - board: x
        max_threads: 60
```

Two rules that matter:

- **Mappings merge; lists replace.** An override naming three subreddits gets
  three, not three appended to the defaults.
- **Unknown keys are rejected.** A typo fails at startup with the path to the
  offending key, rather than silently reverting to a default six hours into a run.

### Environment overrides

| Variable | Overrides |
|---|---|
| `PULPMILL_CONFIG` | Path to an extra config file |
| `PULPMILL_DATA_DIR` | `runtime.data_dir` |
| `PULPMILL_DB_PATH` | `runtime.database.path` |
| `PULPMILL_LOG_LEVEL` | `runtime.logging.level` |

Relative paths resolve against the project root, so nothing is tied to a
machine-specific absolute path.

---

## `runtime`

```yaml
runtime:
  data_dir: var
  database:
    path: var/pulpmill.db
    busy_timeout_ms: 5000
    journal_mode: WAL       # keeps `status` readable while `run` writes
    synchronous: NORMAL
    foreign_keys: true
  logging:
    level: INFO             # console
    console_format: pretty  # pretty | json
    file:
      enabled: true
      path: var/logs/pulpmill.jsonl
      level: DEBUG
      max_bytes: 10485760   # 10 MiB
      backup_count: 5       # ⇒ at most ~60 MiB of logs on disk
```

Console and file levels are independent: a quiet terminal with full DEBUG detail
on disk is the intended setup for unattended running. Set
`console_format: json` when piping to a log collector.

---

## `http`

Applies to every adapter; per-source rate limits are set separately.

```yaml
http:
  user_agent: "pulpmill/0.1.0 (local story pipeline)"
  timeout:
    connect_seconds: 5.0
    read_seconds: 20.0
    write_seconds: 10.0
    pool_seconds: 5.0
  pool:
    max_connections: 8                 # small on purpose: this shares one laptop
    max_keepalive_connections: 4
    keepalive_expiry_seconds: 30.0
  retry:
    max_attempts: 4
    initial_backoff_seconds: 1.0
    max_backoff_seconds: 60.0
    multiplier: 2.0
    jitter_ratio: 0.2                  # set 0 for fully deterministic backoff
    retry_on_status: [408, 429, 500, 502, 503, 504]
    respect_retry_after: true
    max_retry_after_seconds: 120.0
```

Only the listed statuses are retried. `401`/`403`/`404` fail immediately —
retrying an auth or not-found error is pure noise against a source.
`max_retry_after_seconds` is the point at which an enormous `Retry-After` is
treated as "give up" rather than blocking a worker for an hour.

---

## `ingestion`

```yaml
ingestion:
  max_stories_per_source: 120   # hard ceiling per run
  max_pages_per_query: 4
  stop_on_exhausted_page: true
```

These bound both memory and request volume. `--limit` and `--pages` override
them per invocation.

---

## `deduplication`

```yaml
deduplication:
  layers:
    exact_source: true
    canonical_url: true
    content_hash: true
    near_duplicate:
      enabled: true
      algorithm: simhash64
      hamming_threshold: 6
      min_tokens: 40
      band_count: 4
```

| Setting | Effect |
|---|---|
| `hamming_threshold` | Bits two fingerprints may differ by. **3** = near-identical only. **6** (default) catches a word swapped throughout plus an appended "Edit:" line. **>10** starts merging distinct stories; hard-capped at 12. |
| `min_tokens` | Below this, fingerprints are unstable and the layer abstains rather than guessing. |
| `band_count` | Index selectivity. Recall is provably complete below `band_count` and best-effort above it. |

Each layer can be disabled independently. After changing anything here, run
`pulpmill dedupe` to re-check stored stories — it only ever *adds* duplicate
links, never un-marks one.

---

## `ranking`

### Version and fingerprint

```yaml
ranking:
  version: "2026.08.1"
```

Bump `version` when scoring *behaviour* changes. Scores are stored per
`(story, ranking_version, config_fingerprint)`, and the fingerprint is a hash of
this whole section — so changing a weight writes new rows rather than
overwriting scores produced under the old configuration.

`pulpmill rank` skips stories already scored under the current pair. Use
`--force` to recompute.

### Weights

```yaml
ranking:
  weights:
    engagement: 0.22
    recency: 0.12
    comment_activity: 0.08
    narrative_suitability: 0.26
    length: 0.12
    novelty: 0.12
    source_quality: 0.08
```

Weights are **ratios** — they are normalized, so they need not sum to 1.0.
Setting one to `0` disables that signal.

A signal a platform cannot supply is *dropped and its weight redistributed*, not
scored zero. That is why 4chan threads compete fairly despite having no score
field.

Rough guidance:

| Want | Try |
|---|---|
| Better narration quality | ↑ `narrative_suitability`, ↑ `length` |
| Fresher stories | ↑ `recency`, ↓ `recency.half_life_hours` |
| Less repetitive output | ↑ `novelty` |
| Trust specific communities | ↑ `source_quality`, add `quality_overrides` |
| Chase raw popularity | ↑ `engagement` (note: correlates with reposts) |

### Signal settings

```yaml
  recency:
    half_life_hours: 36.0     # score halves every 36h
    max_age_hours: 336.0      # beyond 14 days, contributes exactly 0

  length:
    floor_words: 120          # trapezoid: 0 below floor …
    ideal_min_words: 320      # … ramping to 1 here …
    ideal_max_words: 900      # … flat to here …
    ceiling_words: 2200       # … decaying to 0 here
```

The length band targets 40–90s narration at ~150 wpm, with headroom above for
stories the series splitter will eventually cut into parts. Must be strictly
increasing; validated at load.

```yaml
  comment_activity:
    reference_comments_per_hour: 25.0   # scores 0.5 at this rate
    min_age_hours: 1.0                  # ignore the first hour — it is noise

  novelty:
    lookback_stories: 400     # how many recent stories to compare against
    shingle_size: 3
    min_tokens: 20
    compare_chars: 1200       # title + this much body; bounds memory
```

`compare_chars` is a memory control. Comparing full bodies against 400 stories
would be tens of megabytes held continuously.

```yaml
  narrative_suitability:
    first_person_weight: 0.22
    dialogue_weight: 0.14
    conflict_weight: 0.20
    temporal_structure_weight: 0.16
    paragraph_structure_weight: 0.12
    title_hook_weight: 0.16
    link_heavy_penalty: 0.35
    shouting_penalty: 0.15
    meta_post_penalty: 0.40
```

Cues sum (capped at 1.0), then penalties are subtracted. Every cue and penalty
appears in `pulpmill inspect`, so a surprising score is always traceable.

This is a **heuristic, not a virality predictor** — it measures whether text
looks narratable, not whether a story is good.

---

## `editorial`

```yaml
editorial:
  provider: deterministic     # deterministic | claude
  candidate_pool_size: 10     # how many top candidates the provider sees
  select_count: 5             # how many it must return
  claude:
    model: claude-opus-5
    max_output_tokens: 8000
    timeout_seconds: 60.0
    max_attempts: 2
    recent_selection_hours: 72.0
```

`deterministic` needs no key, no network and no model — and is the fallback for
`claude` on any failure. `candidate_pool_size` is the funnel: a provider never
sees more than this many stories, which is what keeps cost bounded.

`max_output_tokens` caps thinking *and* response text together on current
models, so it needs headroom beyond the size of the JSON expected back.

---

## `sources`

Each source has a core section the pipeline understands and an adapter-owned
section it does not interpret:

```yaml
sources:
  reddit:
    enabled: true
    adapter: reddit               # registry key
    rate_limit:
      requests_per_second: 1.0
      burst: 2
    quality: 0.80                 # baseline, 0-1
    quality_overrides:            # keyed by the story's `quality_key`
      nosleep: 1.00
      AmItheAsshole: 0.95
    engagement:
      score_reference: 4000.0     # a story here scores ~0.5 on that axis
      comment_reference: 300.0
    filters: {...}                # ← validated by the adapter
    queries: [...]                # ← validated by the adapter
    options: {...}                # ← validated by the adapter
```

`engagement` references are per-platform because 4,000 Reddit upvotes and 80
4chan replies are comparable amounts of attention in incomparable units. **A
`null` reference means the platform does not report that metric** — the ranking
engine drops the axis rather than scoring it zero. That is why
`sources.fourchan.engagement.score_reference` is `null`.

`quality_overrides` is looked up by whatever the adapter records as the story's
`quality_key` — subreddit for Reddit, board for 4chan, `@handle` for X. The
ranking signal performs a dictionary lookup and never learns what a subreddit is.

### Per-source specifics

**Reddit** — `queries` take `subreddit`, `listing` (`hot`/`new`/`top`/`rising`),
`time_filter`, `limit`. `filters` take `min_score`, `min_body_chars`,
`max_body_chars`, `allow_nsfw`, `skip_stickied`, `require_selftext`.

**4chan** — `queries` take `board`, `max_threads`. `filters` take
`min_body_chars`, `max_body_chars`, `min_replies`, `skip_sticky`.
`options.fetch_full_thread` costs one extra request per accepted thread but
gets the authoritative OP body. Keep `requests_per_second` at 1.0 — the API
documentation mandates it.

**X** — disabled by default. Read [docs/SOURCES.md](docs/SOURCES.md) before
enabling; every request is billed.

---

## Secrets

Only in `.env` (git-ignored). See [docs/CREDENTIALS.md](docs/CREDENTIALS.md).

| Variable | Used by |
|---|---|
| `PULPMILL_REDDIT_CLIENT_ID` / `_SECRET` | Reddit (required) |
| `PULPMILL_REDDIT_USER_AGENT` | Reddit (strongly recommended) |
| `PULPMILL_REDDIT_AUTH_MODE` / `_USERNAME` / `_PASSWORD` | Reddit script-app grant (optional) |
| `PULPMILL_X_BEARER_TOKEN` | X (optional, paid) |
| `ANTHROPIC_API_KEY` | Claude editorial (optional) |

A blank value reads as unset, so a `.env` copied from `.env.example` does not
look configured. The logger redacts anything whose key resembles a credential,
and there is no command that prints a secret value.

---

## Common changes

**Add a subreddit** — `config/pipeline.local.yaml`:

```yaml
sources:
  reddit:
    queries:
      - {subreddit: nosleep, listing: top, time_filter: week, limit: 50}
      - {subreddit: LetsNotMeet, listing: top, time_filter: month, limit: 25}
      - {subreddit: TalesFromTechSupport, listing: top, time_filter: week, limit: 25}
```

(Lists replace, so include the ones you want to keep.)

**Scrape more per run**

```yaml
ingestion:
  max_stories_per_source: 300
  max_pages_per_query: 8
```

**Prefer longer stories for multi-part videos**

```yaml
ranking:
  version: "2026.08.2"      # bump — behaviour changed
  length:
    ideal_min_words: 800
    ideal_max_words: 2000
    ceiling_words: 5000
```

**Be gentler on a source**

```yaml
sources:
  reddit:
    rate_limit:
      requests_per_second: 0.5
```
