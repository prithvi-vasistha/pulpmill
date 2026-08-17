# Credentials — what still needs an account

**This is the file to open when you sit down to do the logins.** Everything the
pipeline needs from you, in one place, with the exact variable names.

Check current state at any time:

```bash
pulpmill config secrets     # reports which variables are set — never prints a value
pulpmill sources            # reports whether each adapter can actually fetch
```

Nothing here blocks the pipeline. 4chan works today with no account at all, and
`pulpmill run` skips any source whose credentials are missing rather than
failing.

---

## Status summary

| Source | Account needed | Cost | State today |
|---|---|---|---|
| 4chan | No | Free | **Working now.** Public read-only API. |
| Reddit | **Yes** | Free (non-commercial) | Adapter complete, waiting on credentials. |
| X / Twitter | Yes | **Paid, ~$0.005 per post read** | Adapter complete, disabled by design. |
| Claude editorial | Optional | Paid per token | Optional. Falls back to local ranking. |

---

## 1. Reddit — the one worth doing

**Why it's required:** Reddit's anonymous JSON endpoints
(`www.reddit.com/r/<sub>/hot.json`) return **HTTP 403** as of 2026. Verified
against the live host during development, with a browser-style User-Agent and
with Reddit's own documented agent format. OAuth is the only supported read
path, so the adapter implements it properly rather than trying to work around
the block.

### Steps

1. Sign in to Reddit with the account you want the bot associated with.
2. Go to **https://www.reddit.com/prefs/apps** and click
   **"create another app..."** at the bottom.
3. Fill in:
   - **name**: `pulpmill`
   - **type**: select **script** ← important, not "web app"
   - **description**: anything
   - **about url**: leave blank
   - **redirect uri**: `http://localhost:8080` (unused for script apps, but the
     form requires a value)
4. Click **create app**.
5. Copy two values from the resulting box:
   - **client id** — the short string directly *under* the app name, near
     "personal use script". It is not labelled.
   - **secret** — the field labelled `secret`.

### Then put them in `.env`

```bash
cp .env.example .env    # if you have not already
```

```ini
PULPMILL_REDDIT_CLIENT_ID=<the string under the app name>
PULPMILL_REDDIT_CLIENT_SECRET=<the secret field>
PULPMILL_REDDIT_USER_AGENT=linux:pulpmill:0.1.0 (by /u/YOUR_REDDIT_USERNAME)
```

**Set the user agent properly.** Reddit documents this exact format and
throttles generic agents hard. Replace `YOUR_REDDIT_USERNAME` with your actual
username.

### Verify

```bash
pulpmill sources                              # reddit should read "ready"
pulpmill run --source reddit --limit 5        # a real fetch
```

### Notes

- **Auth mode.** The default `client_credentials` grant is app-only and
  read-only — it needs no account password and is all this pipeline uses. Only
  set `PULPMILL_REDDIT_AUTH_MODE=password` (plus `PULPMILL_REDDIT_USERNAME` and
  `PULPMILL_REDDIT_PASSWORD`) if a future stage needs to act *as* the user.
- **Rate limits.** Free tier is 100 queries/minute per OAuth client, averaged
  over ten minutes. The default config requests 1 rps, and the adapter also
  reads Reddit's `X-Ratelimit-Remaining` header and stalls itself when the
  budget runs low.
- **Terms.** Reddit's free Data API tier is for non-commercial use, and since
  the November 2025 Responsible Builder Policy it expects registration even for
  personal projects. Commercial use requires a paid agreement. Worth knowing
  before this becomes a monetised channel.

---

## 2. X / Twitter — read the cost before enabling

**Do not enable this without deciding to spend money.**

X discontinued its free read tier. As of 6 February 2026 new developers are on
pay-per-use billing at roughly **$0.005 per post read**, with no free allowance.
The legacy Basic ($200/mo) and Pro ($5,000/mo) tiers are closed to new signups.

At 500 posts a day that is ~$75/month, for a source whose content fits
short-form narration poorly (see [SOURCES.md](SOURCES.md)).

The adapter is fully implemented against the official API v2 recent-search
endpoint. If you decide to pay:

1. Create a project and app at **https://developer.x.com**.
2. Under **Keys and tokens**, generate a **Bearer Token** (OAuth 2.0 App-Only).
3. Set it in `.env`:
   ```ini
   PULPMILL_X_BEARER_TOKEN=<bearer token>
   ```
4. Enable the source in `config/pipeline.local.yaml`:
   ```yaml
   sources:
     x:
       enabled: true
   ```

Without the token, `pulpmill sources` reports X as unavailable and explains why.
That is deliberate: the alternative would be pretending the source works.

---

## 3. Claude editorial selection — optional

Only needed if you want an editorial pass over the top-ranked candidates. With
no key, `pulpmill select` uses deterministic ranking order and says so.

1. Get a key from **https://console.anthropic.com** → API keys.
2. Set it under the SDK's conventional name:
   ```ini
   ANTHROPIC_API_KEY=sk-ant-...
   ```
3. Install the optional dependency and switch the provider on:
   ```bash
   uv sync --extra claude
   ```
   ```yaml
   # config/pipeline.local.yaml
   editorial:
     provider: claude
   ```

Cost is small by design — only 5–20 candidates are ever sent, never the scraped
dataset. If the call fails for any reason (timeout, rate limit, invalid JSON, a
hallucinated story id), the pipeline falls back to ranking order and records the
reason on the batch.

---

## Security rules

- `.env` is git-ignored. Keep it that way; `.env.example` is the committed template.
- Secrets never appear in `config/pipeline.yaml` — that file is committed.
- The logger redacts anything whose key looks like a credential, and
  `Authorization` / `Cookie` headers are masked before any log line is written.
- `pulpmill config secrets` reports presence only. There is no command that
  prints a secret value.
- If a credential leaks, revoke it at the source first (Reddit app settings,
  X developer portal, Anthropic console), then rotate `.env`.
