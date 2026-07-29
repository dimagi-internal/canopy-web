# Canopy brand assets

The canopy mark — a bare-branch tree — rendered into every image the product ships.
These files are **first-class committed assets**: consumers read them, they are never
drawn just-in-time at build or runtime.

## The one source of truth

The tree's *shape* is defined once, as line segments, in
[`tree.py`](tree.py).
Everything here is rendered from that geometry by [`generate.py`](./generate.py). To
change the mark, edit `tree.py`, then:

```bash
python assets/brand/generate.py     # macOS only (AppKit + iconutil)
```

…and commit the regenerated files. **Do not hand-edit the outputs** — the next
regenerate overwrites them.

## What's here

| File | Used by | Notes |
|------|---------|-------|
| `tree.svg` | canonical vector | white tree on black; identical to the web favicon |
| `menubar-tree.png` / `@2x` / `@3x` | macOS menu-bar app | monochrome; the app **tints it per runner status** (green=running, amber=paused, red=stopped) |
| `app-icon-1024.png` | macOS app-icon artwork | green tree on a warm-earth tile |
| `AppIcon.icns` | macOS `.app` bundle | all sizes, folded from the 1024 art |

`generate.py` also refreshes the **web** copies in place, so the whole image set has
one generator:

| File | Used by |
|------|---------|
| `frontend/public/favicon.svg` | web `<link rel=icon>` |
| `frontend/public/icons/icon-192.png`, `icon-512.png` | PWA manifest |
| `frontend/public/icons/icon-maskable-512.png` | PWA maskable (safe-area padded) |

## Why committed, not generated on demand

The mark used to be re-rendered at three different call sites (menu-bar icon, PWA
icons, the `.app` icns) — three renderers that could drift, and a build that shelled
out to Python to draw an icon every time. Committing the outputs makes the images a
stable dependency: the menu-bar app and CI just read files, and the mark changes only
when someone deliberately edits `tree.py` and regenerates.
