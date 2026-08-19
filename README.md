# pulpmill

Local-first pipeline that turns public internet stories into short-form vertical
video.

It queries multiple sources, normalizes what it finds into one story model,
removes duplicates deterministically, scores everything with a transparent
weighted ranking, writes narration scripts, synthesises speech locally, renders
captioned 1080×1920 video with ffmpeg, validates the result, and publishes it.
It runs entirely on one machine and needs no AI service at any stage.

```
SOURCE ─▶ FETCH ─▶ NORMALIZE ─▶ DEDUPE ─▶ PERSIST ─▶ RANK ─▶ SELECT
                                                                │
   PUBLISH ◀─ VALIDATE ◀─ RENDER ◀─ NARRATE ◀─ SCRIPT ◀─────────┘
```

**Status:** every stage from discovery through validation runs end to end today.
Publishing adapters for YouTube, Instagram and TikTok are implemented and tested
but ship disabled — each platform gates on an approval process that takes weeks.
See [docs/PUBLISHING.md](docs/PUBLISHING.md).

---

## Quick start

```bash
# 1. Install (uv manages the Python version and the virtualenv)
uv sync

# 2. Discover. 4chan needs no account, so this works immediately.
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

### Making videos

```bash
# One-time: local speech synthesis (~200 MB of packages, ~340 MB of weights)
uv sync --extra tts
./scripts/fetch-tts-model.sh
uv run pulpmill assets          # confirm ffmpeg, encoder and TTS are ready

# Then, per batch
uv run pulpmill select          # choose what to publish
uv run pulpmill produce         # script → narrate → render → validate
uv run pulpmill publish --target youtube --limit 1   # dry run; --live to transmit
```

No gameplay footage yet? Nothing breaks. `render.background.mode: auto` uses a
generated animated gradient until clips appear in `assets/backgrounds/`, then
switches to them on the next render with no configuration change.

Everything is safe to re-run. Scraping the same post twice updates one row;
re-ranking an unchanged story is a no-op; and an unchanged script resolves to
the audio already on disk without touching the model.

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
| `pulpmill policy` | Check stored stories against the content-policy blocklist |
| `pulpmill renormalize` | Recompute text, hashes and fingerprints after a normalizer change |
| `pulpmill select` | Pick the next publication batch from the top candidates |
| `pulpmill script` | Turn selected stories into narration scripts and numbered parts |
| `pulpmill narrate` | Synthesise narration audio with word timings |
| `pulpmill render` | Compose vertical video from narration, captions and a background |
| `pulpmill validate` | Check rendered files against the publishability rules |
| `pulpmill produce` | All four production stages in order |
| `pulpmill publish` | Publish validated videos. Dry run unless `--live` |
| `pulpmill relink` | Backfill series cross-links into published descriptions |
| `pulpmill targets` | Publishing targets and what each one still needs |
| `pulpmill assets` | ffmpeg, encoder, TTS and background-clip readiness |
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
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | Platform approval gates, quotas, metadata |
| [docs/CONTENT_POLICY.md](docs/CONTENT_POLICY.md) | Which communities are off limits, and why |

---

## Requirements

Linux, Python ≥ 3.12 (uv provides it), SQLite — that is the whole requirement
for discovery and ranking.

Rendering needs **FFmpeg** with libass. An NVIDIA GPU is used automatically when
the build has NVENC and is not required; `render.encoder: auto` falls back to
libx264. Speech synthesis is CPU-only by default, which deliberately leaves the
GPU to the encoder.

Verified on the development machine: FFmpeg 8.1.2, GTX 1660 Ti (h264_nvenc),
Kokoro-82M via ONNX Runtime on CPU at roughly 2× realtime.
