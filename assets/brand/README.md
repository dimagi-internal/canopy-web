# Canopy brand assets

The canopy mark — the **low dome**: a wide, shallow canopy in three segments over a
row of three — rendered into every image the product ships. These files are
**first-class committed assets**: consumers read them, they are never drawn
just-in-time at build or runtime.

It replaced the bare-branch tree on 2026-10-05 (the tree went spindly below 32px).
Why it looks the way it does — and the two shapes it must not drift back into — is in
the docstring of [`mark.py`](mark.py) and in
`docs/superpowers/specs/2026-10-05-public-site-and-mark-design.md`.

## The one source of truth

The mark's *shape* is defined once, in [`mark.py`](mark.py). Everything here is
rendered from it by [`generate.py`](./generate.py). To change the mark, edit
`mark.py`, then:

```bash
brew install librsvg                 # one-time: rsvg-convert
python3 assets/brand/generate.py     # macOS only for the .icns (iconutil)
```

…and commit the regenerated files. **Do not hand-edit the outputs** — the next
regenerate overwrites them.

## What's here

| File | Used by | Notes |
|------|---------|-------|
| `mark.svg` | canonical vector | green mark on the bark tile |
| `menubar-mark.png` / `@2x` / `@3x` | macOS menu-bar app | monochrome, 24×16pt (the mark is wide); the app **tints it per runner status** (green=running, amber=paused, red=stopped) |
| `app-icon-1024.png` | macOS app-icon artwork | green mark on a warm-earth tile |
| `AppIcon.icns` | macOS `.app` bundle | all sizes, folded from the 1024 art |

`generate.py` also refreshes the copies other parts of the repo read:

| File | Used by |
|------|---------|
| `frontend/public/favicon.svg` | web `<link rel=icon>` |
| `frontend/public/icons/icon-192.png`, `icon-512.png` | PWA manifest |
| `frontend/public/icons/icon-maskable-512.png` | PWA maskable (mark kept inside the safe zone) |
| `frontend/src/brand/CanopyMark.tsx` | the in-app headers, in `currentColor` |
| `site/public/favicon.svg`, `site/src/assets/mark.svg` | the public site, when `site/` exists |

Every raster is rendered from the same SVG through `rsvg-convert`, so there is no
second drawing of the shape to drift.

## Why committed, not generated on demand

The mark used to be re-rendered at three different call sites (menu-bar icon, PWA
icons, the `.app` icns) — three renderers that could drift, and a build that shelled
out to Python to draw an icon every time. Committing the outputs makes the images a
stable dependency: the menu-bar app and CI just read files, and the mark changes only
when someone deliberately edits `mark.py` and regenerates.
