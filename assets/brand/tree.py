"""The canopy tree — pure geometry, no AppKit/PyObjC imports.

This is the single source of truth for the "bare-branch tree" mark. Every image the
product ships is rendered from these constants by `assets/brand/generate.py` (the app
icon + .icns, the monochrome menu-bar tree, the web favicon, the PWA icons) — see
`render_tree_svg.py` for the SVG path. The outputs are COMMITTED under `assets/brand/`;
consumers (the native Swift menu-bar app, the web) read those files, never re-render.
One definition, one generator — the images can't drift into different trees.
"""
from __future__ import annotations

import math

# The tree as line segments: (start, end, stroke width). Drawn in arbitrary units —
# consumers measure the result and scale it to fill their target, so these numbers
# only set the SHAPE. Tune angles/lengths freely; the fit is recomputed.
TRUNK_TOP = (9.0, 6.2)
LIMBS = ((36, 5.4), (72, 5.0), (108, 5.0), (144, 5.4))  # (degrees, length) — 4 limbs
FORKS = (-26, 26)  # each limb splits once, at +/- this many degrees
ICON_INSET = 0.03  # breathing room as a fraction of the icon, so it scales with px


def tree_segments() -> list[tuple[tuple[float, float], tuple[float, float], float]]:
    """The bare-branch tree: heavy trunk, four limbs, each forking once. Widths taper
    2.2 -> 1.5 -> 1.0 — that ladder is what keeps the limbs legible at 18px (finer,
    more numerous branching mushes into a blob at menu-bar size)."""
    segs = [((9.0, 1.3), TRUNK_TOP, 2.2)]
    for angle, length in LIMBS:
        tip = (TRUNK_TOP[0] + math.cos(math.radians(angle)) * length,
               TRUNK_TOP[1] + math.sin(math.radians(angle)) * length)
        segs.append((TRUNK_TOP, tip, 1.5))
        for fork in FORKS:
            fa = math.radians(angle + fork)
            segs.append((tip, (tip[0] + math.cos(fa) * 2.6, tip[1] + math.sin(fa) * 2.6), 1.0))
    return segs


def ink_bounds(segs) -> tuple[float, float, float, float]:
    """Bounding box of the stroked segments — round caps mean each end bulges out by
    half the line width, so the geometric bounds alone would undercount."""
    xs = [p[0] + s * w / 2 for a, b, w in segs for p in (a, b) for s in (-1, 1)]
    ys = [p[1] + s * w / 2 for a, b, w in segs for p in (a, b) for s in (-1, 1)]
    return min(xs), min(ys), max(xs), max(ys)
