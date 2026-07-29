#!/usr/bin/env python3
"""Regenerate EVERY canopy brand image from the one geometry source.

The bare-branch tree is defined ONCE as line segments in
`assets/brand/tree.py`. This script renders that geometry
into every committed image the product needs, so nothing is drawn just-in-time at
build or runtime. Edit `tree.py` to change the shape, then run this and commit the
results — never hand-edit the outputs.

    python3 -m venv .venv && .venv/bin/pip install pyobjc-framework-Cocoa  # one-time
    .venv/bin/python assets/brand/generate.py                              # regenerate all

Outputs (all committed, first-class):
    assets/brand/tree.svg                 master vector (white tree on black)
    assets/brand/menubar-tree.png/@2x/@3x monochrome tree for the macOS status bar
                                          (the menu-bar app tints it per runner status)
    assets/brand/app-icon-1024.png        macOS app-icon artwork (green tree, warm tile)
    assets/brand/AppIcon.icns             compiled macOS app icon (all sizes)
    frontend/public/favicon.svg           web favicon  (identical to tree.svg)
    frontend/public/icons/icon-192.png    PWA icon
    frontend/public/icons/icon-512.png    PWA icon
    frontend/public/icons/icon-maskable-512.png  PWA maskable icon

macOS-only: PNG/icns rendering uses AppKit + `iconutil`. Asset generation is a
dev-machine task; the outputs are committed so CI and every consumer just read files.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BRAND = REPO / "assets" / "brand"
WEB_ICONS = REPO / "frontend" / "public" / "icons"
WEB_FAVICON = REPO / "frontend" / "public" / "favicon.svg"

# tree.py (pure geometry) + render_tree_svg (pure SVG) are the shape source of truth.
# They live HERE, beside their only consumer. They used to sit inside
# packages/canopy_runner and were therefore built into the runner wheel and
# installed onto every box in the fleet — 112 lines of icon geometry the daemon
# never imports.
sys.path.insert(0, str(BRAND))
from render_tree_svg import render_svg  # noqa: E402
from tree import ICON_INSET, ink_bounds, tree_segments  # noqa: E402

# Brand palette (canopy Warm Earth) — the ONLY place these hexes live for the mark.
WARM_TILE = (0.16, 0.13, 0.11)   # app-icon background
BRAND_GREEN = (0.40, 0.71, 0.52)  # the tree on the app icon
BLACK = (0.0, 0.0, 0.0)           # menu-bar template ink (tinted per status at runtime)


def _draw_tree(px: int, rgb, *, fill_frac: float, cx_off=0.0, cy_off=0.0):
    """Return an NSImage of the tree in `rgb`, scaled to `fill_frac` of a px-square,
    centered. Shared by every raster output so they can't drift from tree.py."""
    from AppKit import (
        NSAffineTransform, NSBezierPath, NSColor, NSImage, NSMakePoint, NSMakeSize,
        NSRoundLineCapStyle,
    )
    segs = tree_segments()
    x0, y0, x1, y1 = ink_bounds(segs)
    span = max(x1 - x0, y1 - y0)
    scale = px * fill_frac / span
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2

    def _draw(_rect) -> bool:
        NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], 1.0).set()
        xf = NSAffineTransform.transform()
        xf.translateXBy_yBy_(px / 2 + cx_off, px / 2 + cy_off)
        xf.scaleBy_(scale)
        xf.translateXBy_yBy_(-cx, -cy)
        xf.concat()
        for a, b, w in segs:
            p = NSBezierPath.bezierPath()
            p.moveToPoint_(NSMakePoint(*a))
            p.lineToPoint_(NSMakePoint(*b))
            p.setLineWidth_(w)
            p.setLineCapStyle_(NSRoundLineCapStyle)
            p.stroke()
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(px, px), False, _draw)


def _write_png(img, path: Path) -> None:
    from AppKit import NSBitmapImageRep
    rep = NSBitmapImageRep.alloc().initWithData_(img.TIFFRepresentation())
    png = rep.representationUsingType_properties_(4, None)  # 4 = NSPNGFileType
    if not png.writeToFile_atomically_(str(path), True):
        raise RuntimeError(f"failed to write {path}")
    print(f"  wrote {_rel(path)}")


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)  # e.g. iconset staged in a temp dir


def _app_icon(px: int):
    """Green tree on a warm rounded-rect tile — the macOS/Spotlight app icon."""
    from AppKit import (
        NSBezierPath, NSColor, NSImage, NSMakeRect, NSMakeSize,
    )
    tree = _draw_tree(px, BRAND_GREEN, fill_frac=0.52)

    def _draw(_rect) -> bool:
        inset = px * 0.06
        rect = NSMakeRect(inset, inset, px - 2 * inset, px - 2 * inset)
        bg = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, px * 0.225, px * 0.225)
        NSColor.colorWithSRGBRed_green_blue_alpha_(*WARM_TILE, 1.0).set()
        bg.fill()
        tree.drawInRect_(NSMakeRect(0, 0, px, px))
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(px, px), False, _draw)


def _build_icns(png_1024: Path, out: Path) -> None:
    """Fold the 1024 artwork into a full multi-size .icns via macOS iconutil."""
    from AppKit import NSImage
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        base = NSImage.alloc().initWithContentsOfFile_(str(png_1024))
        for px, name in [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
                         (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
                         (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]:
            _write_png(_scaled(base, px), iconset / f"icon_{name}.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print(f"  wrote {_rel(out)}")


def _scaled(img, px: int):
    from AppKit import NSImage, NSMakeRect, NSMakeSize
    out = NSImage.alloc().initWithSize_(NSMakeSize(px, px))
    out.lockFocus()
    img.drawInRect_(NSMakeRect(0, 0, px, px))
    out.unlockFocus()
    return out


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    WEB_ICONS.mkdir(parents=True, exist_ok=True)
    print("Regenerating brand assets from assets/brand/tree.py:")

    # 1. Master SVG + web favicon (pure-python; white tree on black).
    svg = render_svg(512)
    (BRAND / "tree.svg").write_text(svg)
    print(f"  wrote {(BRAND / 'tree.svg').relative_to(REPO)}")
    WEB_FAVICON.write_text(svg)
    print(f"  wrote {WEB_FAVICON.relative_to(REPO)}")

    # 2. Menu-bar tree — monochrome black on transparent, tinted per status at runtime.
    for scale, suffix in [(1, ""), (2, "@2x"), (3, "@3x")]:
        _write_png(_draw_tree(18 * scale, BLACK, fill_frac=1 - 2 * ICON_INSET),
                   BRAND / f"menubar-tree{suffix}.png")

    # 3. App icon artwork + compiled icns.
    _write_png(_app_icon(1024), BRAND / "app-icon-1024.png")
    _build_icns(BRAND / "app-icon-1024.png", BRAND / "AppIcon.icns")

    # 4. PWA icons — white tree on black (matches the historical committed set).
    for px, name in [(192, "icon-192.png"), (512, "icon-512.png"), (512, "icon-maskable-512.png")]:
        # maskable needs safe-area padding (0.8) so the tree survives a circular mask.
        frac = 0.62 if "maskable" in name else 1 - 2 * ICON_INSET
        _write_png(_tree_on_black(px, frac), WEB_ICONS / name)

    print("Done. Commit the changes under assets/brand/ and frontend/public/.")


def _tree_on_black(px: int, frac: float):
    from AppKit import NSBezierPath, NSColor, NSImage, NSMakeRect, NSMakeSize
    tree = _draw_tree(px, (1.0, 1.0, 1.0), fill_frac=frac)

    def _draw(_rect) -> bool:
        NSColor.blackColor().set()
        NSBezierPath.bezierPathWithRect_(NSMakeRect(0, 0, px, px)).fill()
        tree.drawInRect_(NSMakeRect(0, 0, px, px))
        return True

    return NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(px, px), False, _draw)


if __name__ == "__main__":
    main()
