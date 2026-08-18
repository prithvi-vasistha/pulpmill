# Branding assets

## `watermark.png`

Overlaid on every rendered video. Ships **disabled** because the file does not
exist yet:

```yaml
render:
  watermark:
    enabled: false                       # flip to true once the file is here
    path: assets/branding/watermark.png
    position: top-right
    scale: 0.18                          # fraction of the 1080px frame width
    opacity: 0.75
```

Enabling it without the file present is a **hard error at render time**, not a
silently skipped overlay. A batch published without branding is not something to
discover afterwards.

Requirements: PNG with alpha. Roughly 400×400 or wider is plenty — it is scaled
to `scale × 1080` px wide, so about 194 px at the default.

## Banner / channel art

Not used by the renderer. Platform channel art is set once, by hand, on each
platform. Keep the files here if it helps to have them in one place.

---

*Image files in this directory are git-ignored. This README is not.*
