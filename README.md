# pulpmill

Local-first story discovery, deduplication and ranking engine — the ingestion
foundation for an automated short-form video pipeline.

It queries multiple internet sources, normalizes what it finds into one story
model, removes duplicates deterministically, scores every story with a
transparent weighted ranking, persists all of it to SQLite, and shows you the
best candidates. It runs entirely locally and needs no AI service.

```
SOURCE ─▶ FETCH ─▶ NORMALIZE ─▶ DEDUPLICATE ─▶ PERSIST ─▶ RANK ─▶ TOP CANDIDATES
```

**Status:** the ingestion and ranking slice is complete and working. Script
generation, TTS, and rendering are later stages; the interfaces and schema they
plug into already exist. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick start

```bash
# 1. Install (uv manages the Python version and the virtualenv)
uv sync

# 2. Run the pipeline. 4chan needs no account, so this works immediately.
uv run pulpmill run --source fourchan --limit 20

# 3. Look at what it found
uv run pulpmill status
uv run pulpmill top
uv run pulpmill inspect <story-id>
```

To add Reddit — a free account and a two-minute app registration — follow
[docs/CREDENTIALS.md](docs/CREDENTIALS.md), then:

```bash
cp .env.example .env    # fill in the Reddit values
uv run pulpmill sources # confirm reddit reads "ready"
uv run pulpmill run
```

Everything is safe to re-run. Scraping the same post twice updates one row;
re-ranking an unchanged story is a no-op.

---

## Sources

| Source | Acquisition | Account | State |
|---|---|---|---|
| **4chan** | Official read-only JSON API (`a.4cdn.org`) | None | **Working now** |
| **Reddit** | Official OAuth2 Data API (`oauth.reddit.com`) | Free, required | Implemented — [add credentials](docs/CREDENTIALS.md) |
| **X** | Official API v2 recent search | Paid, ~$0.005/read | Implemented, **disabled by default** |

Reddit's anonymous JSON endpoints return HTTP 403 as of 2026, so OAuth is the
only supported path. X retired its free read tier entirely — the adapter is real
but shipping it enabled would be spending your money by default. Full reasoning
in [docs/SOURCES.md](docs/SOURCES.md).

---

## Commands

Only commands that actually work are exposed.

| Command | What it does |
|---|---|
| `pulpmill run` | The whole slice: fetch → normalize → dedupe → persist → rank → show top candidates |
| `pulpmill scrape` | Ingest only, no ranking |
| `pulpmill rank` | Score stored stories; skips ones already scored under the current config |
| `pulpmill dedupe` | Re-check stored stories against current dedup settings |
| `pulpmill renormalize` | Recompute text, hashes and fingerprints after a normalizer change |
| `pulpmill select` | Pick the next publication batch from the top candidates |
| `pulpmill sources` | List sources and whether each can currently fetch |
| `pulpmill status` | Counts by status and source, duplicates by layer, failures, recent jobs |
| `pulpmill top` | Highest-ranked candidates |
| `pulpmill inspect <id>` | One story in full: provenance, content, score breakdown, state history |
| `pulpmill failures` | Recent persisted failures with source, stage and error |
| `pulpmill db upgrade\|status\|verify` | Schema migrations |
| `pulpmill config show\|secrets` | Effective configuration; which credentials are set |

`status`, `top`, `inspect`, `run`, `scrape`, `rank` and `config show` all accept
`--json`.

**Why did this story score 63?**

```bash
uv run pulpmill inspect <story-id>
```

prints every signal's value, its effective weight, its contribution, and the
evidence behind it — matched conflict terms, paragraph count, comment rate, and
so on.

---

## How ranking works

Seven independent signals, each scored 0–1, combined with configurable weights
into a 0–100 score:

| Signal | Measures |
|---|---|
| `engagement` | Attention, normalized per platform |
| `recency` | Exponential decay, configurable half-life |
| `comment_activity` | Discussion *rate*, not raw volume |
| `narrative_suitability` | Lexical cues that text will narrate well |
| `length` | Fit to a narratable duration band |
| `novelty` | Unlikeness to recently discovered stories |
| `source_quality` | Operator-assigned trust per community |

Three properties worth knowing:

- **Deterministic.** Same story + same config + same ranking version + same
  reference time ⇒ same score. The reference time is stored on every ranking row
  so a past result stays reproducible.
- **Weights are ratios.** They need not sum to 1.0; they are normalized.
- **A signal a platform cannot supply is dropped, not zeroed.** 4chan has no
  score field, so its engagement weight is redistributed across the other
  signals instead of permanently penalising every 4chan thread.

`narrative_suitability` is a transparent heuristic, not a virality predictor —
it measures whether text *looks* narratable (first-person voice, dialogue,
conflict, temporal structure) and penalises what plainly is not (link dumps,
all-caps, meta posts). Every cue is reported in the explanation.

Tune it in [`config/pipeline.yaml`](config/pipeline.yaml); see
[CONFIGURATION.md](CONFIGURATION.md).

---

## Deduplication

Four layers, cheapest first, stopping at the first match:

| Layer | Key | Catches |
|---|---|---|
| 1 | `(source_platform, source_id)` | The same post again — refreshed, not re-added |
| 2 | Normalized-URL fingerprint | Same URL via a different id or host alias |
| 3 | Flattened-content SHA-256 | Identical body, **including across platforms** |
| 4 | 64-bit SimHash, banded LSH | Lightly-edited reposts |

Layers 2–4 are what catch a viral story appearing on Reddit and 4chan as one
story. All four are deterministic — no embeddings, no model, no GPU. Semantic
deduplication would slot in behind the same `DedupStrategy` interface.

---

## Design commitments

- **Provenance is never discarded.** `source_platform`, `source_id`,
  `canonical_url`, `author` and `title` survive every transformation, and the
  stored URL is byte-identical to what the source gave us. A rendered video must
  be traceable back to its original post.
- **No AI required.** Nothing in the pipeline depends on an AI service. Claude
  editorial selection is one optional stage behind a provider interface, seeing
  only 5–20 candidates, with deterministic fallback on any failure.
- **Restartable.** Every story is committed as it is processed and every state
  change is recorded. Killing the process loses at most the story in flight.
- **Resource-conscious.** Streaming ingestion, bounded connection pools, keyset
  pagination, a bounded novelty corpus, rotated logs.
- **Nothing gets blocked.** Client-side rate limiting per source, exponential
  backoff with jitter, `Retry-After` honoured, and no attempt to work around
  CAPTCHAs, authentication or anti-bot systems.

---

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module layout, data flow, schema, extension points |
| [CONFIGURATION.md](CONFIGURATION.md) | Every setting, what it does, how to tune it |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Setup, tests, troubleshooting, adding a source |
| [docs/SOURCES.md](docs/SOURCES.md) | Per-source acquisition, limits, and honest caveats |
| [docs/CREDENTIALS.md](docs/CREDENTIALS.md) | **The account setup checklist** |

---

## Requirements

Linux, Python ≥ 3.12 (uv provides it), SQLite. FFmpeg and a CUDA GPU are needed
only by the later rendering and TTS stages, not by anything here.
