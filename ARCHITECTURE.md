# Architecture

How pulpmill is put together, why it is put together that way, and where the
remaining pipeline stages attach.

---

## Layers

Dependencies point inward. `domain/` imports nothing from the outer layers,
which is what lets the ranking engine be tested without a database and the
database be swapped without touching the model.

```
                              cli/                  ← presentation only
                               │
                           pipeline/                ← composition root + runners
     ┌──────────┬──────────┬────┼─────┬──────────┬───────────┬────────────┐
     │          │          │          │          │           │            │
ingestion/ deduplication/ ranking/ editorial/ scripting/    tts/      publishing/
     │          │          │          │          │           │            │
     │          │          │          │          │      captions/   rendering/
     │          │          │          │          │           │       validation/
     └──────────┴──────────┴────┬─────┴──────────┴───────────┴────────────┘
                                │
                   normalization/  persistence/
                                │
                        infrastructure/     ← clock, HTTP, logging, retry
                                │
                            domain/         ← models, enums, contracts, rules
```

| Package | Responsibility |
|---|---|
| `domain/` | Story model, enums, state machine, source/ranking contracts, errors |
| `config/` | Typed configuration, layering, secret access |
| `infrastructure/` | Clock, structured logging, HTTP client, retry policy, rate limiting |
| `normalization/` | Text cleaning, URL canonicalization, content fingerprinting |
| `ingestion/` | Adapter registry and the per-source adapters |
| `deduplication/` | Layered strategies and the engine that applies them |
| `ranking/` | Signals and the weighted scoring engine |
| `editorial/` | Optional AI selection behind a provider interface |
| `scripting/` | Segmentation, speech shaping, hooks, optional AI pacing advice |
| `tts/` | Speech provider interface, Kokoro backends, alignment, track assembly |
| `captions/` | Cue grouping and ASS subtitle generation |
| `rendering/` | Background providers, ffmpeg process layer, video compositor |
| `validation/` | Publishability checks measured against the rendered file |
| `publishing/` | Publisher registry, per-platform adapters, metadata |
| `persistence/` | SQLite connection, migrations, repositories |
| `pipeline/` | Application wiring, stage runners, reports |
| `cli/` | Typer commands and rich rendering |

---

## Data flow

```
 ┌────────┐  RawStory   ┌───────────┐   Story    ┌──────────┐
 │ fetch  ├────────────▶│ normalize ├───────────▶│  dedupe  │
 └────────┘   (lazy)    └───────────┘            └────┬─────┘
   adapter                 adapter                    │
                                                      ▼
                          ┌──────────────────────────────────────┐
                          │ NEW → persist  KNOWN → refresh only   │
                          │ DUPLICATE → persist, link, set aside  │
                          └────────────────┬─────────────────────┘
                                           ▼
                                    ┌────────────┐   ┌──────────────┐
                                    │    rank    ├──▶│ top candidates│
                                    └────────────┘   └──────┬───────┘
                                                            ▼
                                                     ┌────────────┐
                                                     │   select   │  ← editorial
                                                     └─────┬──────┘
                                                           ▼
 ┌──────────┐  NarrationScript  ┌──────────┐ AudioArtifact ┌──────────┐
 │  script  ├──────────────────▶│ narrate  ├──────────────▶│  render  │
 └──────────┘   + StoryParts    └──────────┘  + timings    └────┬─────┘
  segmentation   speech shaping   per-sentence TTS               │ VideoArtifact
  + hooks        + part numbers   + concatenation                ▼
                                                          ┌────────────┐
                                                          │  validate  │
                                                          └─────┬──────┘
                                                                ▼
                                                          ┌────────────┐
                                                          │  publish   │
                                                          └────────────┘
```

Two properties shape the whole design:

**Streaming.** `fetch` is a generator. Each story is normalized, deduplicated
and persisted before the next is pulled, so peak memory is one story plus a
bounded novelty corpus — not one run's worth of posts. This matters on a 16 GB
laptop that will eventually also be rendering video.

**Per-story commits.** Each story lands in its own transaction and each state
change is recorded as an event. Killing the process mid-run loses at most the
story in flight; the next run resumes from the database.

---

## The canonical story

`domain/story.py`. Immutable — every change returns a new instance via `evolve`,
which **rejects edits to provenance fields**. That is a hard rule, enforced in
code: a rendered video must be traceable back to `video → part → story → source
→ original URL`, and a URL that can be rewritten mid-pipeline breaks the chain.

Story ids are `uuid5(namespace, f"{platform}:{source_id}")`. Deterministic by
construction, which is what makes re-scraping an update rather than a duplicate
insert — on any machine, in any run.

Three derived fingerprints back the dedup layers:

| Field | Derivation | Used by |
|---|---|---|
| `url_fingerprint` | SHA-256 of the *normalized* URL | Layer 2 |
| `content_hash` | SHA-256 of the *flattened* body | Layer 3 |
| `simhash` | 64-bit Charikar SimHash | Layer 4 |

`canonical_url` itself is stored byte-identical to what the source gave us,
trimmed only. Normalization happens on the way to the fingerprint, never to the
stored value.

`Engagement` fields are all optional, and `None` means "this platform does not
report this" — never zero. Conflating the two would permanently penalise every
4chan thread for lacking a score field.

---

## Source adapters

One `SourceAdapter` protocol; everything platform-specific lives behind it.
There is no `if source == "reddit"` anywhere outside `ingestion/adapters/`.

Per-platform differences are handled by two generic mechanisms rather than
branching:

- **`metadata["quality_key"]`** — the adapter writes the community identifier
  (subreddit, board, `@handle`); the `source_quality` signal looks it up in
  `sources.<name>.quality_overrides`. The signal never learns what a subreddit is.
- **`metadata["raw_format"]`** — `markdown` / `html` / `plain`, so
  `renormalize` can re-decode stored text without asking which platform it came
  from.

Adapters validate their own `queries`, `filters` and `options` with their own
Pydantic models. The core config deliberately types those as free-form mappings,
so adding a source never means editing a core model.

See [docs/SOURCES.md](docs/SOURCES.md) for per-source detail and the four-step
guide to adding one.

---

## Deduplication

`DeduplicationEngine` applies `DedupStrategy` implementations cheapest-first and
stops at the first match.

| Layer | Strategy | Verdict | Cost |
|---|---|---|---|
| 1 | `ExactSourceStrategy` | `KNOWN` — same row, refresh it | Indexed lookup |
| 2 | `CanonicalUrlStrategy` | `DUPLICATE` | Indexed lookup |
| 3 | `ContentHashStrategy` | `DUPLICATE` | Indexed lookup |
| 4 | `SimHashStrategy` | `DUPLICATE` | Banded LSH lookup + Hamming check |

Layer 1 is deliberately a different verdict: story ids derive from the source
pair, so a match there *is* the same story, not a duplicate of another one.

**Layer 4 is index-backed, not a scan.** The 64-bit fingerprint is split into
four 16-bit bands stored in `story_simhash_bands`. Two fingerprints within a
small Hamming distance almost always share a band, so candidate lookup is an
indexed equality query. Banding is a recall filter; the exact Hamming distance
makes the decision.

The default threshold of 3 is calibrated against **real ingested stories**, not
synthetic pairs. Measured over r/nosleep and /x/ content: the closest pair of
genuinely *different* stories sits at Hamming distance 5, with a median of 15
across all pairs. Same-genre long-form prose converges in SimHash space — two
first-person horror stories share so much vocabulary that the distinguishing
signal washes out — so the usable margin is far tighter than a "same story, one
word changed" test suggests. A threshold of 6 merged two unrelated nosleep
stories on the very first live run.

At 3 the layer catches reposts that are substantially the same text: identical,
reformatted, prepended-intro, or single-word-substitution (measured 0–3 bits).
It does **not** catch substantially rewritten retellings, and that is the right
trade: a missed duplicate is ranked down by the novelty signal, whereas a false
positive silently destroys a real story. Threshold 3 also keeps the pigeonhole
bound (3 < `band_count`), so LSH recall is provably complete rather than
best-effort — reported by `NearDuplicateConfig.recall_is_guaranteed`.

A story marked `DUPLICATE` is removed from the LSH index so it cannot become the
"original" for a later arrival.

Semantic/embedding deduplication is deliberately **not** implemented. It would
be a fifth `DedupStrategy` appended to the same list — the engine would not
change.

---

## Ranking

`RankingEngine` collects `SignalScore`s and combines them. Each signal is a pure
function of `ScoringContext` — no clock, no database, no network. Everything
time- or corpus-dependent is passed in, which is what makes scoring reproducible.

```
value ∈ [0,1] per signal  ×  normalized weight  →  Σ × 100
```

Two behaviours worth knowing:

- **Weights are normalized**, so they express ratios and need not sum to 1.0.
- **An unavailable signal's weight is redistributed** across the rest rather
  than contributing zero.

Every ranking row stores `final_score`, `component_scores`, `effective_weights`,
a full `explanation` (per-signal evidence), `ranking_version`,
`config_fingerprint` and `reference_time`. The fingerprint is a hash of the
canonicalized ranking config, so changing a weight produces a *new* row rather
than silently overwriting scores produced under the old config.

Adding a signal: implement the protocol, add it to `default_signals()`, add a
weight to config. Nothing else changes.

---

## Editorial selection

A separate, optional stage. The point is the funnel:

```
thousands discovered → dedup → deterministic ranking → 5-20 candidates → provider
```

A provider never sees the scraped dataset — only a bounded candidate projection
(title, community, word count, estimated narration seconds, local score, and a
truncated excerpt).

`DeterministicProvider` is the default *and* the fallback: ranking order, no
network, no credentials, no model. `ClaudeProvider` is optional — the SDK is
imported lazily, so the pipeline installs and runs without it.

Claude output is validated three times: structured-output JSON schema at the
API, Pydantic model on parse, then **semantic** validation — every story id must
be one we actually offered, no duplicates, exact count, positions forming
1..N. A response that invents a story id is rejected, because acting on it would
render a video for a story that does not exist.

On *any* failure — timeout, API error, refusal, truncation, malformed JSON,
invalid id — the stage falls back to deterministic order and records
`provider`, `effective_provider` and `fallback_reason` on the batch. A silent
degradation would otherwise vanish with the log rotation.

---

## Persistence

SQLite. This is a single-machine, single-writer pipeline; a server database
would add an always-on process, a socket, a backup story and a failure mode, and
buy nothing at this scale. WAL mode gives concurrent readers, which is all
`pulpmill status` needs while a run is writing.

### Schema

| Table | Purpose |
|---|---|
| `stories` | The canonical record. Unique on `(source_platform, source_id)` |
| `story_simhash_bands` | Banded LSH index for layer-4 dedup |
| `story_state_events` | Append-only audit of every state transition |
| `story_rankings` | Unique on `(story_id, ranking_version, config_fingerprint)` |
| `jobs` / `job_failures` | Run records and persisted failures |
| `editorial_batches` / `editorial_selections` | Selection results and ordering |
| `story_series` / `story_parts` | Multi-part video structure and offsets |
| `story_scripts` | Narration text. Unique on `(story_id, part_number)` |
| `audio_artifacts` | Synthesised tracks with word timings. Unique on `script_id` |
| `video_artifacts` | Rendered files. Unique on `script_id` |
| `video_validations` | Append-only publishability verdicts |
| `publications` | One attempt per platform. Unique on `(video_id, target)` |

Indexes cover `(source_platform, source_id)`, `url_fingerprint`, `content_hash`,
`status`, `final_score`, `created_at`, `discovered_at`, `series_id`,
`config_fingerprint`, `(target, published_at)` and the LSH band lookup.

Every production table carries `story_id` alongside its immediate parent. That
is redundant normalisation on purpose: it makes the chain from a published video
back to its source URL one join instead of four, and makes an orphaned row
impossible to create.

Idempotency is enforced by the schema, not by convention: the UNIQUE constraints
above are what make re-scraping and re-ranking no-ops rather than duplicate rows.

### Migrations

Numbered `.sql` files in `migrations/`, applied in order, each inside its own
transaction, each recorded with a checksum. Nothing else creates or alters a
table — there is no `CREATE TABLE IF NOT EXISTS` scattered through the
repositories.

Two deliberate details: statements are split with `sqlite3.complete_statement`
rather than `executescript`, because `executescript` issues an implicit COMMIT
that would break a migration out of its transaction; and checksums are verified
on every startup, so editing an applied migration is an error rather than a
silent schema divergence between machines.

---

## State machine

`domain/state.py` holds one transition table. States past `RANKED` are declared
now so the schema and machine need not change when the later workers arrive.

```
DISCOVERED → NORMALIZED → DEDUPLICATED → RANKED → SELECTED
                   │            │                     │
                   └─▶ DUPLICATE│                     ▼
                                └─▶ REJECTED    SCRIPT_PENDING → SCRIPT_READY
                                                      ▼
                                    AUDIO_PENDING → AUDIO_READY
                                                      ▼
                                    VIDEO_PENDING → VIDEO_READY → VALIDATED → PUBLISHED
```

`FAILED` is reachable from any non-terminal state, and `FAILED`, `DUPLICATE` and
`REJECTED` have recovery edges back into the pipeline. `RANKED → RANKED` is
explicitly legal — that is what makes re-running `rank` idempotent rather than an
error. A test asserts every declared state is reachable from `DISCOVERED`, so a
stranded state fails the build.

---

## Production

Discovery ends at `SELECTED`. Production takes it to a validated file on disk.

### Script

`scripting/`. A story becomes one or more narration scripts. Three things happen
that are easy to conflate and are kept separate on purpose:

**Segmentation** cuts a long story into parts. `plan_segments` proposes ranges;
`domain.series.plan_parts` turns them into numbered parts. Neither consults a
model.

**Speech shaping** rewrites text that a synthesiser reads wrong. `ScriptLine`
carries both `text` (what captions display) and `speech_text` (what is narrated),
which is what lets a caption show `$1,250` while the narrator says "one thousand
two hundred fifty dollars". This is not cosmetic: the expansion changes duration
by up to 40%, so **planning is done on the spoken form**. Planning on the written
form produced parts that reliably overran.

**Framing** adds a hook and an outro, neither of which exists in the source.

A provider may *advise* — a better hook, better cut points — and its advice is
validated against the actual sentence list and discarded whole if anything is out
of range. A story that cannot be told within `max_parts` is **rejected, not
truncated**: publishing six parts of a seventeen-part story strands the viewer.

### Narrate

`tts/`. One clip per sentence, then concatenation.

That ordering is the whole trick. Sentence boundaries come from *measured* clip
lengths, so they are exact; only the distribution of words inside a sentence is
estimated (by character weight plus a punctuation pause bonus). The alternative —
one long clip subdivided arithmetically — drifts visibly over a minute of video.
The usual fix is a second model for forced alignment; this costs no extra
dependency and is deterministic. `ForcedAligner` is left as a seam for the day
word-level precision matters more than it does now.

Kokoro-82M runs locally behind `TTSProvider`, with both the ONNX and PyTorch
distributions supported. Neither is a hard dependency. Two levels of content-keyed
caching mean a re-scripted story re-synthesises only the lines that changed.

### Render

`captions/` + `rendering/`. One ffmpeg invocation per video:

```
background → scale to cover → crop to frame → fps → grain
           → burn in captions (ASS) → watermark overlay
```

Captions are burned in because no short-form platform displays soft subtitles.
Word highlighting is one ASS event per word — the format's own karaoke mode
colours every *already-spoken* word, which is the wrong look.

**Nothing scraped reaches a command line.** Caption and title text travel in an
ASS file, and `{`, `}` and `\` are escaped out of it, so a story body containing
`{\an8}` cannot reposition the captions.

The background comes from a `BackgroundProvider`. In `auto` mode it uses the clip
library when it holds usable footage and a generated animated gradient when it
does not — which is what makes "everything except the gameplay footage" a
working state rather than a broken one. Clip and start offset are chosen by
`blake2b` of the story id, so renders reproduce and consecutive videos do not
open on the same frame.

### Validate

`validation/`. Measured against the file with ffprobe and ffmpeg, not against
the pipeline's beliefs about the file — the failures worth catching are exactly
the ones where every stage reported success. Duration, dimensions, file size,
audio presence, mean volume, clipping. Every check records its measured value
whether it passed or failed.

### Publish

`publishing/`. Same registry shape as the source adapters. Detail in
[docs/PUBLISHING.md](docs/PUBLISHING.md); the load-bearing parts:

- A video with no passing validation row is refused.
- `UNIQUE (video_id, target)` makes a retry an update, not a second upload.
- The attempt row is written *before* anything is transmitted.
- `dry_run` defaults to on, needs no credentials, and transmits nothing.

### Rules the production stages inherit

- **Provenance travels with the artifact.** Every script, audio track and video
  carries `Provenance`, so a rendered file knows its source URL without a
  database round trip.
- **The pipeline computes part numbers, not a model.** A model may propose where
  to cut; it may not decide that a story is "Part 2 of 4".
- **Stages advance whole stories.** A three-part story reaches `AUDIO_READY`
  only when all three parts have audio. Advancing per part would let a
  half-rendered story look ready to publish.

---

## Cross-cutting

**Time** is always injected via the `Clock` protocol. Nothing calls
`datetime.now()` or `time.sleep()` directly, which is what makes recency scoring
deterministic and backoff testable without waiting.

**HTTP** goes through one client that owns timeouts, bounded pooling, per-source
token-bucket rate limiting, retries with jittered exponential backoff,
`Retry-After` handling, and `429` back-pressure applied to the whole source
rather than the single failed request. It does not attempt to work around
blocks: a `403` is raised, logged, and left for a human.

**Logging** is structured, to a pretty console sink and a rotated JSON-lines
file. A redaction processor masks anything resembling a credential before any
renderer sees it. Loggers are created lazily — binding at import time would
freeze them against structlog's default config and their records would never
reach the file sink.

**Errors** are typed and carry structured context (source, story, stage,
operation, retry count). Failures are *persisted* to `job_failures`, not just
logged, so a week of unattended running can be audited with a query.
