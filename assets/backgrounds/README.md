# Background footage

Drop gameplay clips here as `.mp4`, `.mov`, `.mkv` or `.webm`.

The renderer picks one up automatically on the next run — no configuration
change needed. Until then `render.background.mode: auto` falls back to a
generated animated gradient, so the pipeline produces complete, watchable videos
with this directory empty.

## What works well

| | |
|---|---|
| Length | **20 s minimum** (`min_clip_seconds`), 2–10 minutes is ideal |
| Aspect | Anything. Clips are scaled to cover and centre-cropped to 1080×1920 |
| Content | Continuous motion, no cuts, no on-screen text or HUD |
| Audio | Irrelevant — the clip's audio track is discarded |

A clip shorter than the video loops, which is fine for continuous footage and
obvious for anything with a beginning and an end.

## How a clip gets chosen

Deterministically, from the story id: the same story always renders against the
same clip at the same start offset, so a re-render reproduces byte-for-byte
inputs. Consecutive videos get different clips and different offsets, so a batch
does not look like one template repeated.

## Check what is detected

```bash
uv run pulpmill assets
```

Each file is probed once per run. Anything unreadable or shorter than
`min_clip_seconds` is reported and skipped — one bad file never stops a night's
rendering.

## Licensing

Whatever goes here is redistributed inside every video published from it. Use
footage you have the right to use.

---

*Video files in this directory are git-ignored. This README is not.*
