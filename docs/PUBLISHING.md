# Publishing

Everything needed to get a rendered video onto a platform, and an honest account
of what stands in the way.

The code is finished. All three adapters are fully implemented against the
documented APIs, exercised by tests, and ship **disabled**. What is not finished
— and cannot be finished by writing code — is the approval process each platform
requires. That is the critical path, and it is measured in weeks.

Start the applications before you need them.

## The safety interlock

```yaml
publishing:
  dry_run: true
```

While this is true, every adapter builds the complete request, validates it,
records it in the `publications` table, and transmits nothing. `pulpmill publish`
is a dry run unless you pass `--live`.

A dry run does **not** require credentials, which makes it useful now:

```bash
pulpmill publish --target youtube --limit 1    # rehearse
pulpmill targets                               # readiness per platform
```

Turning `dry_run` off, and passing `--live`, are two separate deliberate acts.
Publishing is the only irreversible thing this pipeline does.

## What is checked before anything is transmitted

In order, and all of it before a single byte leaves the machine:

1. **Validation.** A video with no passing `video_validations` row is refused.
   Everything upstream is recoverable; a bad public upload is not.
2. **The local daily cap.** `daily_limit` per target, counted from real
   publications in the last 24 hours. Dry runs do not consume it.
3. **Already published.** `UNIQUE (video_id, target)` means a retry finds the
   existing attempt instead of uploading a second copy.
4. **The attempt row is written first**, so a process that dies mid-upload
   leaves a record naming exactly which video and which platform were in flight.

## YouTube

**The quota is the constraint, not the code.**

| | |
|---|---|
| Cost of one upload | **1600 units** (`videos.insert`) |
| Default daily allowance | **10,000 units** |
| Uploads per day | **6** |

Six per day is the ceiling until Google approves an increase. That approval is
an audit form with a human reviewer, and it can be refused. If the target is
~29 videos/day, apply early and expect it to be the long pole.

Two further platform rules the adapter reports rather than hides:

- **Unverified projects upload private.** YouTube overrides the requested
  privacy status until the OAuth project passes verification. The adapter
  compares what it asked for against what came back and says so in the result.
- **Unpublished consent screens expire tokens weekly.** An OAuth app left in
  "testing" invalidates refresh tokens after 7 days, which kills a 24/7 worker
  every week. Publish the consent screen.

### Setup

1. Create a project in the [Google Cloud console](https://console.cloud.google.com/).
2. Enable **YouTube Data API v3**.
3. Configure the OAuth consent screen and **publish it** (not "testing").
4. Create an **OAuth client ID** of type *Desktop app*.
5. Run the OAuth flow once with scope `https://www.googleapis.com/auth/youtube.upload`
   and keep the refresh token.
6. Apply for a quota increase if you need more than 6 uploads/day.

```bash
PULPMILL_YOUTUBE_CLIENT_ID=...
PULPMILL_YOUTUBE_CLIENT_SECRET=...
PULPMILL_YOUTUBE_REFRESH_TOKEN=...
```

## Instagram

**Meta does not accept an upload.** It fetches the file from a URL you supply:

```
POST /{ig-user-id}/media   media_type=REELS, video_url=https://...   -> container
GET  /{container-id}?fields=status_code                              -> poll until FINISHED
POST /{ig-user-id}/media_publish   creation_id=...                   -> published
```

That has an architectural consequence for a local-first pipeline: the rendered
file must be reachable over HTTPS from Meta's servers for the duration of the
fetch. `options.public_base_url` is where that gets configured, and the adapter
refuses to run without it rather than failing three steps in.

Publishing also requires the `instagram_content_publish` permission, which
requires **App Review** of a Business or Creator account.

### Setup

1. Convert the Instagram account to **Business** or **Creator**.
2. Create an app in the [Meta developer console](https://developers.facebook.com/).
3. Add the Instagram product and request `instagram_content_publish`.
4. Pass App Review.
5. Arrange HTTPS hosting for `var/video/` and set `public_base_url`.

```bash
PULPMILL_INSTAGRAM_ACCESS_TOKEN=...
PULPMILL_INSTAGRAM_USER_ID=...
```

Rate limit: roughly 50 posts per rolling 24 hours.

## TikTok

**Unaudited apps can only post privately.** The Content Posting API forces
`SELF_ONLY` visibility until the app passes a content-posting audit, and it
enforces that server-side whatever `privacy_level` the request asks for. The
adapter reports the discrepancy instead of recording a public post that isn't.

```
POST /v2/post/publish/video/init/    -> publish_id + signed upload URL
PUT  {upload_url}                     -> the bytes, with a Content-Range header
POST /v2/post/publish/status/fetch/   -> poll until it leaves PROCESSING
```

### Setup

1. Register an app at the [TikTok developer portal](https://developers.tiktok.com/).
2. Add the **Content Posting API** product.
3. Complete the OAuth flow for the `video.publish` scope.
4. Apply for the content-posting audit.

```bash
PULPMILL_TIKTOK_ACCESS_TOKEN=...
```

## Metadata

Built by `publishing/metadata.py`, identically for every platform, then trimmed
to each platform's limits.

- **Title** — the tidied story title, with `[Part 2/4]` at the *front* for a
  series. A truncated title loses its tail, and the part number is the part a
  scrolling viewer needs first.
- **Description** — title, part label, attribution line, hashtags, and the
  source URL. Deliberately spare: a longer description means reproducing more
  of someone's post than the video already does.
- **Attribution survives truncation.** `VideoMetadata.truncated` trims the body
  and never the source link.

Attribution is not permission. It makes a takedown a conversation rather than a
strike; it does not make the underlying use licensed. See
[CONTENT_POLICY.md](CONTENT_POLICY.md).

## Series cross-linking

A story longer than `script.max_seconds` becomes several videos, and each one's
description carries a linked index of the others:

```
Part 2 of 3.

Watch the full story:
Part 1: https://www.youtube.com/shorts/...
Part 3: https://www.youtube.com/shorts/...
```

**Ordering makes this a two-step problem.** Part one is published before part
two exists, so at the moment it goes up there is nothing to link forward to.
Each part therefore links whatever was already live when it was published —
part two links part one, part three links parts one and two — and part one is
left incomplete.

`pulpmill relink` fills in the rest once a series is fully published:

```bash
pulpmill relink                 # dry run: report what would change
pulpmill relink --live          # rewrite the descriptions
pulpmill relink --story <id>    # just one story
```

It is idempotent: a second run finds nothing to change, so it is safe to
schedule. `videos.update` costs 50 quota units against the same allowance an
upload spends 1600 of, which is what makes relinking a back catalogue
affordable.

**YouTube only.** Instagram and TikTok publish captions immutably — there is no
edit endpoint on either — so their earlier parts keep pointing backwards. That
is reported in the relink summary as "platform cannot edit" rather than being
silently skipped.

## Adding a platform

The same four steps as adding a source:

1. Write an adapter in `publishing/adapters/` implementing `Publisher`.
2. Call `register_publisher("name", YourPublisher)` at module scope.
3. Import it in `_load_builtin_publishers`.
4. Add a target block under `publishing.targets` in `config/pipeline.yaml`.

Nothing in the service, the CLI or the pipeline needs to change. There is no
`if target == "youtube"` anywhere, and adding one must not introduce the first.

Make `health()` honest. Every one of these platforms has an approval gate that
no amount of retrying will clear, so "what unblocks this" is more useful than
"what failed".
