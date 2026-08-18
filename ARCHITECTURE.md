# Architecture

How pulpmill is put together, why it is put together that way, and where the
remaining pipeline stages attach.

---

## Layers

Dependencies point inward. `domain/` imports nothing from the outer layers,
which is what lets the ranking engine be tested without a database and the
database be swapped without touching the model.

```
                      cli/            ← presentation only
                        │
                    pipeline/         ← composition root + stage runner
        ┌───────────────┼───────────────┬──────────────┐
        │               │               │              │
   ingestion/      deduplication/   ranking/      editorial/
        │               │               │              │
        └───────────────┴───────┬───────┴──────────────┘
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
| `persistence/` | SQLite connection, migrations, repositories |
| `pipeline/` | Application wiring, stage runner, reports |
| `tts/` | TTS provider interface + working mock (for the narration stage) |
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
                                    └────────────┘   └──────────────┘
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
| `story_series` / `story_parts` | Multi-part video support (schema ready, unused) |

Indexes cover `(source_platform, source_id)`, `url_fingerprint`, `content_hash`,
`status`, `final_score`, `created_at`, `discovered_at`, `series_id` and the LSH
band lookup.

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

## Where the remaining stages attach

Tonight's slice ends at ranking. The rest attaches without rewriting it:

| Stage | Attachment point | Already in place |
|---|---|---|
| Editorial selection | `EditorialProvider` | ✅ implemented, optional |
| Script generation | New stage reading `SELECTED` stories | `SCRIPT_PENDING`/`SCRIPT_READY` states |
| Series splitting | `domain/series.plan_parts` | ✅ implemented + `story_parts` table |
| TTS | `TTSProvider` | ✅ interface + working mock + cache-key design |
| Subtitle timing | `SpeechResult.word_timings` | ✅ in the model |
| Gameplay assets | New module | — |
| FFmpeg rendering | New module | FFmpeg 8.1 with nvenc verified on this machine |
| Publishing | New module | `PUBLISHED` state |

Two rules the later stages inherit:

- **Provenance travels with the artifact.** `StoryPart` carries `Provenance`
  explicitly, so a part that has been through script generation, TTS and
  rendering still knows its source URL.
- **The pipeline computes part numbers, not a model.** `plan_parts` takes cut
  points and assigns `part_number`/`total_parts`; a model may propose *where* to
  cut, never that a story is "Part 2 of 4".

`KokoroProvider` is not stubbed. There is no audio stage to feed yet, and a stub
pretending to synthesise audio would be worse than an honest absence.
`MockTTSProvider` is real and testable — it writes a valid WAV of the estimated
duration with evenly-distributed word timings, which is enough to build the
subtitle and composition stages against.

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
