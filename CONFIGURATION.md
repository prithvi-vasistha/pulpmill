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
      hamming_threshold: 3
      min_tokens: 40
      band_count: 4
```

| Setting | Effect |
|---|---|
| `hamming_threshold` | Bits two fingerprints may differ by. **3** (default) is calibrated on real data: the closest pair of genuinely *different* same-genre stories measures 5, so 3 leaves a two-bit margin and gives zero false positives. **Raising this is dangerous** — 6 merged two unrelated nosleep stories on the first live run, because same-genre long-form prose converges in SimHash space. Re-measure against real content before changing it. |
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

## `script`

How a story becomes a narration script.

| Setting | Default | Notes |
|---|---|---|
| `provider` | `deterministic` | `claude` adds hook and pacing advice, with fallback |
| `version` | `2026.08.1` | Bump when script behaviour changes; stored on every script |
| `words_per_minute` | `185.0` | **Measured**, not assumed — see below |
| `target_seconds` | `75.0` | Aimed-for length of one part |
| `min_seconds` | `15.0` | Shorter than this and the story is refused |
| `max_seconds` | `90.0` | A story longer than this splits. Also any part's ceiling |
| `max_parts` | `10` | A story needing more is **rejected, not truncated** |
| `include_hook` / `include_outro` | `true` | Framing lines the pipeline writes |
| `outro_template` | `"Part {next_part} is up next."` | `{next_part}`, `{total_parts}` |
| `final_outro` | `"Follow for more stories like this."` | Used on the last part |

**`words_per_minute` is the only thing connecting a planned part length to a
real one, so it is measured rather than guessed.** Across the first eight
Kokoro-narrated scripts, `af_heart` ran at 194–206 wpm on ordinary Reddit prose
and 118–150 wpm on acronym-dense board text where letters are spoken
individually. 185 tracks the target corpus while staying under the fast cases.
**Re-measure after changing voice or speed** — `validation` will catch the
overrun either way, but only after a video has been rendered.

**`target_seconds` and `max_seconds` decide how many videos you get.** At the
shipped values a story under 90s is one video, and anything longer splits into
roughly 75-second parts. Measured over the current 71-story corpus: 36 single
part, 20 two-part, 13 three-part, 2 longer — 125 videos in total.

**Rejection is intentional.** If a story cannot fit in `max_parts` even at
`max_seconds` per part, the script stage moves it to `REJECTED` rather than
publishing a series that never finishes. Raise `max_parts` or `max_seconds` if
you would rather have it.

---

## `tts`

| Setting | Default | Notes |
|---|---|---|
| `provider` | `kokoro` | `mock` writes silence of the right length, for testing |
| `voice` | `af_heart` | Prefix encodes accent and gender: `a`/`b`, `f`/`m` |
| `speed` | `1.0` | Playback rate multiplier |
| `language` | `en-us` | espeak-ng code. **Must carry a region** — bare `en` is rejected |
| `sample_rate` | `24000` | Kokoro's native rate |
| `cache_dir` | `var/audio` | Clips and assembled tracks, keyed by content |
| `sentence_gap_seconds` | `0.28` | Silence inserted between sentences |
| `paragraph_gap_seconds` | `0.45` | Longer pause at a paragraph break |
| `max_clip_seconds` | `300.0` | Refuses an implausibly long clip from a looping model |
| `max_words_per_chunk` | `70` | Longest single utterance sent to the model — see below |
| `kokoro.model_path` | `var/models/kokoro-v1.0.onnx` | ONNX backend only |
| `kokoro.voices_path` | `var/models/voices-v1.0.bin` | ONNX backend only |
| `kokoro.device` | `auto` | `cuda` is available to the PyTorch backend |

Weights are not vendored. Run `./scripts/fetch-tts-model.sh` once.

**`max_words_per_chunk` is a hard model limit, not a preference.** Kokoro
truncates past 510 phonemes and then raises `IndexError`; measured against
`af_heart`, that begins at about 85 spoken words. Any sentence longer than this
is subdivided at clause boundaries before synthesis. Run-on posts with no
sentence punctuation are common on 4chan and in low-effort Reddit posts, so this
is a normal input rather than an edge case — and without the subdivision such a
post both crashes narration *and* forces a part past `max_seconds`, because a
sentence is the smallest unit part planning can cut at.

The gaps are not only cosmetic: caption grouping treats a pause as a hard break,
so shortening them below ~0.18s makes captions run sentences together.

---

## `captions`

Colours are ASS `&HAABBGGRR` strings — **alpha, blue, green, red**. That byte
order is the format's, not a typo.

| Setting | Default | Notes |
|---|---|---|
| `enabled` | `true` | |
| `font_family` | `DejaVu Sans` | Must be installed and findable by libass |
| `font_size` | `72` | See the sizing note below |
| `primary_colour` | `&H00FFFFFF` | White |
| `highlight_colour` | `&H0000D7FF` | Amber, in BGR |
| `outline_width` | `5.0` | Heavy outline; captions sit over moving footage |
| `karaoke` | `true` | Highlights the word being spoken |
| `max_words_per_cue` | `4` | |
| `max_chars_per_cue` | `22` | See the sizing note below |
| `min_cue_seconds` | `0.4` | Shorter cues merge into their neighbour |
| `vertical_position` | `0.62` | Fraction of frame height, from the top |
| `horizontal_margin` | `0.08` | Kept clear of platform UI on both sides |

**Sizing:** `font_size` and `max_chars_per_cue` are a pair. 1080px less two 8%
margins is ~908px of usable width, and bold DejaVu Sans averages ~0.55em per
character — so 22 characters at 72px is about 870px. Raise either without
recomputing that and cues wrap to two lines.

---

## `render`

| Setting | Default | Notes |
|---|---|---|
| `width` / `height` | `1080` / `1920` | Must be portrait and even |
| `fps` | `30` | |
| `encoder` | `auto` | Probes ffmpeg once; prefers NVENC, falls back to libx264 |
| `quality` | `23` | `-cq` for NVENC, `-crf` for x264 |
| `max_bitrate` | `5M` | Empty disables the cap. See below |
| `loudness_lufs` | `-14.0` | What the platforms normalise to anyway |
| `output_dir` | `var/video` | |
| `timeout_seconds` | `900.0` | A render exceeding this is killed |

**`max_bitrate` matters more than it looks.** Constant quality alone produces
6+ Mbps on animated gradients with grain — well past the point of visible
improvement, and it makes every upload slower.

### `render.background`

| Setting | Default | Notes |
|---|---|---|
| `mode` | `auto` | `library` when clips exist, `procedural` when they do not |
| `library_dir` | `assets/backgrounds` | Drop `.mp4` files here |
| `min_clip_seconds` | `20.0` | Shorter clips loop too visibly |
| `randomise_start` | `true` | Deterministic per story, so renders reproduce |
| `procedural.top_colour` / `.bottom_colour` | `#141726` / `#2b1035` | Gradient |
| `procedural.grain` | `0.06` | Stops large flat gradients banding |

`auto` is what makes "no footage yet" a working state. Add clips and the next
render uses them, with no configuration change.

### `render.watermark`

Ships **disabled**. Enabling it without the file present is a hard error at
render time, never a silently skipped overlay — a batch published without
branding is not something to discover afterwards.

---

## `validation`

The gate that stops a bad batch reaching a platform. Defaults are strict.

| Setting | Default | Notes |
|---|---|---|
| `min_seconds` / `max_seconds` | `12.0` / `179.0` | 179s is publishable everywhere |
| `max_bytes` | 300 MiB | |
| `require_audio` | `true` | |
| `min_mean_volume_dbfs` | `-45.0` | A silent track is usually a muxing mistake |
| `require_expected_dimensions` | `true` | Must match `render.width`/`height` |
| `duration_tolerance_seconds` | `1.5` | Rendered length vs narration length |
| `enforce_script_part_limit` | `true` | Also check each part against `script.max_seconds` |
| `part_limit_tolerance_seconds` | `8.0` | Headroom before an overrun is a failure |

`enforce_script_part_limit` exists because planning uses an *estimated* speaking
rate. A part can be planned inside `script.max_seconds` and still narrate past
it, and without this check that overrun is invisible until someone watches the
video.

`min_mean_volume_dbfs` is what catches the worst failure mode: a video that
looks completely fine and has no narration. The mock TTS provider writes
silence, so a video made with it fails here — deliberately.

---

## `publishing`

| Setting | Default | Notes |
|---|---|---|
| `dry_run` | `true` | **Global interlock.** Builds and records; transmits nothing |
| `attribution_template` | `"Source: {url}"` | Appended to every description |
| `targets.<name>.enabled` | `false` | Every target ships disabled |
| `targets.<name>.privacy` | `private` | A public default plus a bug is unrecoverable |
| `targets.<name>.daily_limit` | varies | Local cap, independent of the platform's |
| `targets.<name>.hashtags` | varies | Normalised, de-duplicated, first one joins the title |
| `targets.<name>.options` | `{}` | Adapter-owned, validated by the adapter |

`publish` is a dry run unless given `--live`, and a dry run needs no credentials
at all — the whole path is testable before any platform approval exists.

YouTube's `daily_limit` defaults to 6 because that is what the default API quota
allows: `videos.insert` costs 1600 of 10,000 units per day. Raising the number
here does not raise the quota. See [docs/PUBLISHING.md](docs/PUBLISHING.md).

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

### `blocked_quality_keys`

Communities a source must never ingest, matched against the story's
`quality_key` metadata — the subreddit for Reddit, the board for 4chan. One
mechanism, no adapter branching, and case-insensitive because Reddit treats
`r/NoSleep` and `r/nosleep` as the same subreddit.

```yaml
sources:
  reddit:
    blocked_quality_keys:
      - nosleep       # original fiction; authors retain and enforce rights
      - LetsNotMeet   # first-person accounts of real, identifiable people
```

Enforced by the core **before a story is persisted**, so it holds even if a
blocked community is left in `queries`. Editing this does not touch stories
already stored — run `pulpmill policy --apply` for that, which is a deliberate
second step rather than a side effect of editing YAML.

Reasoning for each entry is in [docs/CONTENT_POLICY.md](docs/CONTENT_POLICY.md).

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
