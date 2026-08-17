# Source adapters

What each source is, how it is acquired, what its limits are, and how to add
another one.

For the account/credential steps, see [CREDENTIALS.md](CREDENTIALS.md).

---

## The contract

Every source implements `SourceAdapter` (`src/pulpmill/domain/source.py`):

```python
class SourceAdapter(Protocol):
    @property
    def platform(self) -> str: ...
    def health(self) -> AdapterHealth: ...
    def fetch(self, request: FetchRequest) -> Iterator[RawStory]: ...
    def normalize(self, raw: RawStory) -> Story | None: ...
    def close(self) -> None: ...
```

Four properties matter:

- **`fetch` yields lazily.** The runner processes each story fully — normalize,
  deduplicate, persist — before pulling the next. Peak memory is one story, not
  one run's worth.
- **`normalize` returning `None` is not an error.** It means "valid record, not
  usable as narration": removed by moderators, link-only, below the length
  floor. These are counted as *filtered*. A genuinely malformed payload raises
  `SourceResponseError` and is counted as a *failure*.
- **`health` never raises.** A source with no credentials reports itself
  unavailable, with remediation text, and the run continues without it.
- **Platform-specific behaviour stays inside the adapter.** Nothing downstream
  branches on the platform name.

### How per-platform differences are handled without branching

Two mechanisms, both generic:

| Need | Mechanism |
|---|---|
| Per-community quality weight | Adapter writes `metadata["quality_key"]` (subreddit, board, `@handle`); the ranking signal looks it up in `sources.<name>.quality_overrides`. The signal never learns what a subreddit is. |
| Re-decoding stored text later | Adapter writes `metadata["raw_format"]` (`markdown` / `html` / `plain`); `pulpmill renormalize` reads it. No `if platform == ...` anywhere. |

Metrics a platform does not report are `None`, never `0` — see the engagement
note under 4chan.

---

## Reddit

**Acquisition:** official OAuth2 Data API at `oauth.reddit.com`.
**Status:** implemented; needs credentials.
**Enabled by default:** yes (skipped at runtime until credentials exist).

Anonymous JSON endpoints return **HTTP 403** as of 2026 — verified live, with
several User-Agents. OAuth is the only supported read path. The adapter does not
attempt to work around the block.

| Aspect | Detail |
|---|---|
| Auth | `client_credentials` (app-only, read-only) by default; `password` grant available for a script app |
| Token handling | Cached, refreshed 60s before expiry, retried once if rejected mid-run |
| Rate limit | 1 rps configured; free tier allows 100 QPM per client |
| Adaptive backoff | Reads `X-Ratelimit-Remaining` / `-Reset` and stalls its own bucket when the budget is nearly spent |
| Pagination | `after` cursor, bounded by `ingestion.max_pages_per_query` |
| Body format | Markdown → stripped to prose (link text kept, targets dropped) |
| Filters | `min_score`, `min_body_chars`, `max_body_chars`, `allow_nsfw`, `skip_stickied`, `require_selftext` |

**Captured fields:** `source`, `source_id` (`t3_...` fullname), `canonical_url`,
`author`, `subreddit`, `title`, `body`, `created_at`, `score`, `comment_count`,
`permalink`.

**The permalink is stored exactly as Reddit gave it** — `permalink_base_url` +
`data.permalink`, trimmed only, never reconstructed. Deduplication compares a
derived `url_fingerprint` instead, so normalization never touches the stored URL.

Skipped automatically: `[removed]` / `[deleted]` bodies, `removed_by_category`
set, stickied posts, link/image posts with no selftext, and — loudly — any record
missing an id or permalink, because a story we cannot attribute is worse than a
story we do not have.

---

## 4chan

**Acquisition:** official read-only JSON API at `a.4cdn.org`
([docs](https://github.com/4chan/4chan-API)).
**Status:** working now, no account required.
**Enabled by default:** yes.

The documentation states three hard rules, all honoured:

| Rule | Implementation |
|---|---|
| ≤ 1 request/second | Token bucket at 1 rps (`sources.fourchan.rate_limit`) |
| Send `If-Modified-Since` | Sent per URL once a `Last-Modified` is known; `304` is treated as "nothing new", not an error |
| GET/HEAD/OPTIONS only | Only GET is used |

| Aspect | Detail |
|---|---|
| Discovery | `/{board}/catalog.json` |
| Full body | `/{board}/thread/{no}.json` when `fetch_full_thread: true` (catalog OP text can be abbreviated) |
| Canonical URL | `https://boards.4chan.org/{board}/thread/{no}` |
| Body format | HTML → text; `<br>` becomes newlines, entities decoded after tags are stripped |
| Default boards | `/x/` (paranormal) and `/adv/` (advice) — the worksafe, story-dense ones |

**Preserved:** board, thread id, post id, canonical URL, author identity where
one exists, timestamp, text, reply and image counts.

**Its metadata semantics are not forced into Reddit's shape:**

- **No score exists.** `engagement.score` is `None`, and
  `engagement.score_reference` is `null` in config. The ranking engine drops
  that axis and redistributes its weight rather than scoring the platform zero
  for a metric it cannot emit.
- **Anonymous by default.** A tripcode is a real persistent identity and is
  stored; a per-thread poster ID is stored as `ID:xxxxxxxx`; a plain
  "Anonymous" is stored as *no author* rather than a fake one.
- **Often no subject line.** The first sentence of the OP becomes the title, and
  `metadata["title_derived"] = true` records that it was derived.
- **Quotelinks are stripped** (`>>12345` reads as noise in narration).
  **Greentext is deliberately kept** — it carries the story's voice.

Threads pruned between the catalog read and the thread fetch are normal; the
adapter logs it and falls back to the catalog entry.

---

## X / Twitter

**Acquisition:** official API v2 recent search, OAuth 2.0 App-Only bearer token.
**Status:** implemented, **disabled by default**.
**Enabled by default:** no — and this is a billing decision, not a technical one.

### The honest assessment

There is no free, stable, appropriate acquisition path for X content:

- The free read tier was discontinued. As of 6 February 2026, new developers are
  on pay-per-use at roughly **$0.005 per post read**, with no free allowance.
  Legacy Basic ($200/mo) and Pro ($5,000/mo) are closed to new signups.
- The only supported interface is the documented API v2. That is what the
  adapter implements.
- Scraping x.com HTML, using undocumented internal GraphQL endpoints, or driving
  logged-in session cookies would all mean working around authentication and
  anti-bot controls. **Not implemented.**

### A second, non-financial limitation

A tweet is a poor fit for narration. Recent search returns *posts, not threads*,
and the long-form storytelling on X lives in multi-post threads that recent
search does not assemble. The default query and the 240-character minimum
reflect that. Reassembling threads would need `conversation_id` fan-out — more
billed reads per story, on top of the per-read cost.

The adapter is real: it authenticates, paginates via `next_token`, expands
`author_id` into a username for the canonical URL, and maps
likes/replies/quotes/retweets/impressions onto the engagement model. Without a
token, `health()` reports unavailable and says why. That is the honest state.

---

## Adding a source

Four steps, none of which touch the core:

1. **Write the adapter** in `src/pulpmill/ingestion/adapters/<name>.py`
   implementing the protocol. Validate its own `queries`/`filters`/`options`
   config with its own Pydantic models — the core deliberately leaves those as
   free-form mappings.
2. **Register it** at the bottom of the module:
   ```python
   register_adapter("myservice", MyServiceAdapter)
   ```
   and import it in `adapters/__init__.py`.
3. **Set the shared metadata** in `normalize`: `QUALITY_KEY` so per-community
   quality lookup works, and `RAW_FORMAT_KEY` so renormalization works.
4. **Add a config block** under `sources:` naming your adapter.

Nothing in `domain/`, `ranking/`, `deduplication/`, `persistence/` or `cli/`
changes. `tests/integration/test_pipeline.py` proves this by registering a
synthetic adapter at runtime and driving the whole pipeline through it.

### Rules for a well-behaved adapter

- Use the injected `HttpClient` — you get timeouts, retries, exponential
  backoff, `429`/`Retry-After` handling and rate limiting for free.
- Respect the source's documented limits, and make them configurable.
- Never work around CAPTCHAs, authentication, or anti-bot systems. A source you
  cannot access legitimately is a source you report as unavailable.
- Preserve the canonical URL byte-for-byte.
- Yield lazily and honour `request.limit` and `request.max_pages`.
- Return `None` for unusable records; raise `SourceResponseError` for malformed
  ones. The distinction drives the *filtered* vs *failures* counts.
