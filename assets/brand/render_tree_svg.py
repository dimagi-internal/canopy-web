"""Render the canopy tree (see `tree.py`) to an SVG string — the PWA-icon consumer
of the shared geometry, sibling to `menubar.py::_tree_image`'s AppKit rendering.

Reproduces `_tree_image`'s fit logic exactly: `span = max(width, height)` of the ink
bounds, `scale = px * (1 - 2*ICON_INSET) / span`, centred at the icon's middle.

One wrinkle AppKit doesn't have: SVG's y-axis points DOWN, `tree_segments()`'s points
UP (drawn the way `_tree_image` draws it, with `NSImage.imageWithSize_flipped_drawingHandler_`'s
`flipped=False`, i.e. a standard bottom-up Cartesian space). Rendered verbatim into
SVG the tree would appear upside-down, so every y is negated before scale/translate —
equivalent to a `scale(1,-1)` flip about the ink's own centre.

Run standalone: `python assets/brand/render_tree_svg.py <out.svg> [px]`.
"""
from __future__ import annotations

import sys

from tree import ICON_INSET, ink_bounds, tree_segments

BG = "#000000"
STROKE = "#ffffff"


def render_svg(px: int = 512) -> str:
    """An SVG string of the tree, white-on-black, fit to a `px`-square icon."""
    segs = tree_segments()
    x0, y0, x1, y1 = ink_bounds(segs)
    span = max(x1 - x0, y1 - y0)
    scale = px * (1 - 2 * ICON_INSET) / span
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    lines = []
    for (ax, ay), (bx, by), w in segs:
        # Flip y (SVG points down, the tree's geometry points up), then apply the same
        # centre-scale-translate _tree_image uses to fit the ink to the icon box.
        sx1 = (ax - cx) * scale + px / 2
        sy1 = (-ay + cy) * scale + px / 2
        sx2 = (bx - cx) * scale + px / 2
        sy2 = (-by + cy) * scale + px / 2
        sw = w * scale
        lines.append(
            f'<line x1="{sx1:.3f}" y1="{sy1:.3f}" x2="{sx2:.3f}" y2="{sy2:.3f}" '
            f'stroke="{STROKE}" stroke-width="{sw:.3f}" stroke-linecap="round"/>'
        )

    body = "\n  ".join(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{px}" height="{px}" '
        f'viewBox="0 0 {px} {px}">\n'
        f'  <rect width="{px}" height="{px}" fill="{BG}"/>\n'
        f'  {body}\n'
        f'</svg>\n'
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python assets/brand/render_tree_svg.py <out.svg> [px]", file=sys.stderr)
        raise SystemExit(2)
    out_path = sys.argv[1]
    px = int(sys.argv[2]) if len(sys.argv) > 2 else 512
    with open(out_path, "w") as f:
        f.write(render_svg(px))
    print(f"wrote {out_path} ({px}x{px})")


if __name__ == "__main__":
    main()
