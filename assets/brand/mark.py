"""The canopy mark — pure geometry, no rendering dependencies.

The single source of truth for the "low dome": a wide, shallow canopy in three
separate segments over a row of three — many parts making one cover, over the
people and agents working beneath it. Chosen 2026-10-05 over three rounds on the
brand canvas (https://claude.ai/artifact/DgjhaGr3AX1gJejmXDT833, variant "G4").

Two lessons are baked into the numbers, so tune them knowingly:

- The dome is LOW (a 120° slice of a big circle, centred below the mark), not a
  semicircle. An even semicircle split in three with a dot at its centre reads as a
  speedometer — that is what killed the variant before this one.
- The segments are separated by real gaps with butt caps. Round caps closed the gaps
  at 16px and the three pieces welded back into one arc.

Every image the product ships is rendered from `svg()` by `generate.py`; the outputs
are committed. One definition, one generator — the images can't drift.
"""
from __future__ import annotations

import math

# Design units. Only the SHAPE matters: every consumer fits the ink bounds to its box.
CENTRE = (50.0, 100.0)    # centre of the dome's circle (below the mark)
RADIUS = 60.0
STROKE = 13.0
SEGMENTS = ((150, 113), (107, 73), (67, 30))  # degrees, left to right; 6° gaps
DOTS = (33.0, 50.0, 67.0)  # the row beneath, at y = DOT_Y
DOT_Y = 72.0
DOT_R = 7.0


def _pt(r: float, deg: float) -> tuple[float, float]:
    cx, cy = CENTRE
    return cx + r * math.cos(math.radians(deg)), cy - r * math.sin(math.radians(deg))


def bounds() -> tuple[float, float, float, float]:
    """Ink bounds (x0, y0, x1, y1), y pointing DOWN as in SVG. Butt caps mean an arc's
    extremes are its end edges and its top, so sample both edges of every arc."""
    pts = []
    for a0, a1 in SEGMENTS:
        for i in range(65):
            deg = a0 + (a1 - a0) * i / 64
            for r in (RADIUS - STROKE / 2, RADIUS + STROKE / 2):
                pts.append(_pt(r, deg))
    for x in DOTS:
        pts += [(x - DOT_R, DOT_Y - DOT_R), (x + DOT_R, DOT_Y + DOT_R)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def body(color: str) -> str:
    """The mark's SVG elements in design units (no <svg> wrapper)."""
    out = []
    for a0, a1 in SEGMENTS:
        (x0, y0), (x1, y1) = _pt(RADIUS, a0), _pt(RADIUS, a1)
        out.append(
            f'<path d="M{x0:.3f} {y0:.3f}A{RADIUS:g} {RADIUS:g} 0 0 1 {x1:.3f} {y1:.3f}" '
            f'fill="none" stroke="{color}" stroke-width="{STROKE:g}" stroke-linecap="butt"/>'
        )
    out += [f'<circle cx="{x:g}" cy="{DOT_Y:g}" r="{DOT_R:g}" fill="{color}"/>' for x in DOTS]
    return "".join(out)


def svg(width: float, height: float, color: str, *, fill: float = 1.0,
        background: str | None = None, radius: float = 0.0) -> str:
    """The mark fit to a width×height box: its ink spans `fill` of whichever side binds,
    centred. `background` paints the box first (with corner `radius`, as a fraction of
    the shorter side), for tiles."""
    x0, y0, x1, y1 = bounds()
    scale = min(width * fill / (x1 - x0), height * fill / (y1 - y0))
    tx = width / 2 - scale * (x0 + x1) / 2
    ty = height / 2 - scale * (y0 + y1) / 2
    bg = ""
    if background:
        rx = radius * min(width, height)
        bg = f'<rect width="{width:g}" height="{height:g}" rx="{rx:g}" fill="{background}"/>'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" height="{height:g}" '
        f'viewBox="0 0 {width:g} {height:g}">{bg}'
        f'<g transform="translate({tx:.3f} {ty:.3f}) scale({scale:.5f})">{body(color)}</g></svg>\n'
    )
