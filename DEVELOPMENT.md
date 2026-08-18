# Development

---

## Setup

The project uses [uv](https://docs.astral.sh/uv/), which manages the Python
version and the virtualenv for you.

```bash
# Install uv (no sudo needed; lands in ~/.local/bin)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"     # add to your shell rc

# Install the project and its dev dependencies
uv sync

# Optional: the Claude editorial provider
uv sync --extra claude
```

Everything runs through `uv run`, so no manual venv activation:

```bash
uv run pulpmill --help
uv run pytest
```

---

## Everyday commands

```bash
uv run pytest                                   # full suite (~5s, no network)
uv run pytest tests/unit -q                     # unit only
uv run pytest -k dedup -v                       # one area
uv run pytest --cov=src/pulpmill --cov-report=term-missing

uv run ruff check src/ tests/                   # lint
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/                  # format
uv run mypy                                     # strict type check
```

Before committing:

```bash
uv run ruff check src/ tests/ && uv run ruff format src/ tests/ \
  && uv run mypy && uv run pytest -q
```

---

## Testing philosophy

**No test touches the network.** Adapters are exercised through an injected
`httpx.MockTransport` against recorded payloads in `tests/fixtures/`, so the
real auth, pagination, filtering, normalization and error-handling code runs —
only the socket is replaced.

**Time is injected.** `tests/support/clock.py` provides `ManualClock`, which
records sleeps instead of performing them. Rate-limiting and backoff tests
assert on exact schedules and finish instantly.

**Determinism is asserted, not assumed.** Ranking, deduplication, candidate
ordering, script building, background selection and word timing all have
explicit "same input ⇒ same output" tests.

**One local tool is allowed.** The render tests invoke the real ffmpeg — it is a
tool, not a network service, and mocking it would test nothing worth testing.
They skip cleanly when it is absent, so the rest of the suite runs anywhere.

**Speech synthesis runs through the mock provider.** It writes real WAV files of
the correct length, which is exactly what the timing, caption and muxing logic
needs. It also writes *silence*, which is why the validation test asserts that a
mock-narrated video is refused — the gate catching its own worst failure mode.

### Layout

| Path | Covers |
|---|---|
| `tests/unit/` | Pure logic: normalization, hashing, config, state machine, ranking signals, retry/rate limiting, editorial validation, series planning, speech shaping, segmentation, captions, alignment, publishing metadata, content policy |
| `tests/integration/` | Real SQLite, real adapter code and real ffmpeg: persistence, migrations, dedup, adapters, HTTP client, pipeline, editorial service, production stages, publishing, CLI |
| `tests/fixtures/` | Recorded API payloads |
| `tests/support/` | Deterministic doubles |
| `tests/conftest.py` | Shared fixtures |

### Useful fixtures

| Fixture | Gives you |
|---|---|
| `config` | The **real committed config**, with writes redirected to `tmp_path` |
| `database` | A migrated SQLite database |
| `stories` / `rankings` / `jobs` / `failures` / `editorial` | Repositories |
| `app` | A wired `Application` |
| `make_story(...)` | A canonical `Story` with sensible defaults |
| `clock` | `ManualClock` |

Because `config` loads the file that actually ships, a broken default fails the
suite.

### Adding tests

```python
def test_a_lightly_edited_repost_is_caught(self, config, stories, make_story) -> None:
    original = make_story(source_id="t3_orig")
    stories.upsert(original)

    edited = make_story(
        source_id="t3_edited",
        canonical_url="https://www.reddit.com/r/x/comments/edited/",
        body=original.normalized_content.replace("roommate", "flatmate"),
    )
    verdict = engine_for(config, stories).evaluate(edited)
    assert verdict.outcome is DedupOutcome.DUPLICATE
```

One caution: `make_story()` bodies share a common stem, so several of them *will*
(correctly) trip the near-duplicate layer. When a test needs N genuinely distinct
stories, give each an unrelated `body` — see `DISTINCT_BODIES` in
`tests/integration/test_pipeline.py`.

---

## Adding a source

Four steps, none of which touch the core. Full detail in
[docs/SOURCES.md](docs/SOURCES.md).

1. **Write the adapter** — `src/pulpmill/ingestion/adapters/<name>.py`,
   implementing `SourceAdapter`. Validate its own `queries`/`filters`/`options`
   with its own Pydantic models.
2. **Register it** — `register_adapter("myservice", MyServiceAdapter)` at the
   bottom of the module, and import it in `adapters/__init__.py`.
3. **Set shared metadata** in `normalize` — `QUALITY_KEY` and `RAW_FORMAT_KEY`.
4. **Add a config block** under `sources:`.

`tests/integration/test_pipeline.py` proves this works by registering a
synthetic adapter at runtime and driving the whole pipeline through it.

### Checklist for a new adapter

- [ ] Uses the injected `HttpClient` (timeouts, retries, backoff, rate limiting)
- [ ] `health()` never raises and explains what to do when unavailable
- [ ] `fetch()` yields lazily and honours `limit` and `max_pages`
- [ ] `canonical_url` is stored byte-for-byte from the source
- [ ] `normalize()` returns `None` for unusable records, raises
      `SourceResponseError` for malformed ones
- [ ] Metrics the platform does not report are `None`, not `0`
- [ ] Respects documented rate limits, configurably
- [ ] Does not work around CAPTCHAs, auth, or anti-bot systems
- [ ] Tested with `httpx.MockTransport`, including malformed payloads

---

## Adding a ranking signal

1. Implement the protocol in `src/pulpmill/ranking/signals/`:
   ```python
   class MySignal:
       name = "my_signal"
       def score(self, context: ScoringContext) -> SignalScore:
           return SignalScore(name=self.name, value=0.5, detail={"why": "..."})
   ```
2. Add it to `default_signals()`.
3. Add a weight under `ranking.weights` (a signal without one is rejected at
   startup).
4. **Bump `ranking.version`** — behaviour changed.

Signals must be pure functions of `ScoringContext`. No clock, no database, no
network — that is what keeps scoring reproducible. Put anything time- or
corpus-dependent in the context.

Populate `detail` generously: it is what `pulpmill inspect` shows, and it is the
difference between an auditable score and a magic number.

---

## Adding a migration

```bash
# Next number in sequence, snake_case name
touch migrations/0002_add_something.sql
uv run pulpmill db upgrade
```

Rules, all enforced:

- Filenames are `NNNN_snake_case.sql`, consecutive from `0001`
- **Never edit an applied migration.** Checksums are verified on every startup;
  editing one is an error, not a silent schema divergence between machines
- Each file runs in its own transaction — a failure leaves the last good version
- End every statement with a semicolon

```bash
uv run pulpmill db status    # applied vs pending
uv run pulpmill db verify    # checksums still match
```

---

## Project layout

```
config/pipeline.yaml          committed defaults
config/pipeline.local.yaml    your overrides (git-ignored)
migrations/                   numbered SQL migrations
src/pulpmill/
  domain/                     models, enums, contracts, rules
  config/                     typed config + secret access
  infrastructure/             clock, logging, HTTP, retry, rate limiting
  normalization/              text, URL, fingerprints
  ingestion/                  registry + adapters
  deduplication/              layered strategies + engine
  ranking/                    signals + scoring engine
  editorial/                  optional AI selection
  scripting/                  segmentation, speech shaping, hooks
  tts/                        speech providers, alignment, track assembly
  captions/                   cue grouping + ASS generation
  rendering/                  backgrounds, ffmpeg, compositor
  validation/                 publishability checks
  publishing/                 registry + platform adapters
  persistence/                database, migrations, repositories
  pipeline/                   wiring, runners, reports
  cli/                        commands + rendering
assets/backgrounds/           gameplay footage (git-ignored, empty by default)
assets/branding/              watermark and banner assets
scripts/                      quality gate, TTS model download
tests/                        unit, integration, fixtures, support
var/                          runtime data — db, logs, audio, video, models (git-ignored)
```

---

## Working on production stages

The stages have very different costs, which is why each is a separate command.

```bash
uv run pulpmill script                     # seconds
uv run pulpmill narrate --provider mock    # instant; writes silence
uv run pulpmill narrate                    # ~2x realtime on CPU
uv run pulpmill render --limit 1           # ~20s per minute of video with NVENC
uv run pulpmill validate
```

Iterating on caption styling does **not** require re-narrating: audio is cached
by content, so `render --force` reuses the tracks. Iterating on the hook does
not require re-synthesising the body: clips are cached per line.

To inspect what a render actually produced:

```bash
ffmpeg -ss 12 -i var/video/<script-id>.mp4 -frames:v 1 /tmp/frame.png
ffmpeg -i var/video/<script-id>.mp4 -af volumedetect -f null -   # audio sanity
cat var/render/<script-id>.ass                                   # the captions
```

The `.ass` file is the fastest way to debug caption timing — it is plain text
with real timestamps, and libass renders exactly what it says.

---

## Troubleshooting

**`pulpmill: command not found`** — use `uv run pulpmill`, or add
`~/.local/bin` to `PATH` for `uv` itself.

**Reddit shows "unavailable"** — expected until you add credentials. Run
`pulpmill config secrets`; see [docs/CREDENTIALS.md](docs/CREDENTIALS.md).

**Reddit returns 403** — anonymous endpoints are blocked as of 2026 and OAuth is
mandatory. If it happens *with* credentials, check the client id/secret and that
`PULPMILL_REDDIT_USER_AGENT` follows the documented format.

**`pulpmill top` says nothing is ranked** — either nothing has been ingested
(`pulpmill status`), or the ranking config changed. Scores are stored per
`(version, config_fingerprint)`; a changed weight means existing rows no longer
match. Run `pulpmill rank`.

**Everything is being marked duplicate** — likely `hamming_threshold` set too
high. It is capped at 12; above ~10 distinct stories start merging. Inspect a
case with `pulpmill inspect <id>` and check `duplicate_of`.

**A run is slow** — mostly deliberate. 4chan is limited to 1 request/second by
its API rules, and `fetch_full_thread` adds one request per accepted thread. The
logs show `rate_limit_wait` when the limiter is the cause.

**Config change had no effect** — check `pulpmill config show` for the effective
values, confirm you edited `config/pipeline.local.yaml`, and remember that lists
replace rather than merge.

**A migration fails** — the database is left at the last good version. Fix the
SQL and re-run `pulpmill db upgrade`. If you edited an applied migration, revert
it and add a new one instead.

**Where are the logs** — `var/logs/pulpmill.jsonl`, rotated at 10 MiB × 5.

```bash
tail -f var/logs/pulpmill.jsonl | jq -c '{ts:.timestamp, lvl:.level, ev:.event}'
jq -c 'select(.level=="error")' var/logs/pulpmill.jsonl
```

Failures are also persisted and queryable: `pulpmill failures`.

---

## Conventions

- **Type hints everywhere.** `mypy` runs in strict mode and is expected to pass.
- **Pydantic at the boundaries** (config, model output), **dataclasses in the
  core.**
- **Injected dependencies.** Clock, HTTP transport, repositories and providers
  are all passed in — that is what makes the suite fast and deterministic.
- **Typed errors with context.** Never `except Exception: pass`. Catch a
  specific error, record a `job_failures` row, or let it propagate.
- **Comments explain *why*.** The code already says what.
- **Provenance is not negotiable.** If a change would let `canonical_url`,
  `source_id` or `source_platform` be rewritten mid-pipeline, it is wrong —
  `Story.evolve` rejects it at runtime, and there is a test.
