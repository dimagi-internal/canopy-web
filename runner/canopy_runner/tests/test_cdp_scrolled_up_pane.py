"""A pane scrolled up is not a pane with no composer — a STATIC guard on emdash_control.mjs.

The sidecar reads the composer from `.xterm-rows`, which is only what the terminal's
view shows. Someone who scrolled an emdash pane up to read a long reply left it
there, and every chat send into that session bounced COMPOSER_NOT_VISIBLE however
often it was retried: turn 6c185edd (2026-10-26), four claims over 90s, each reading
the identical mid-reply frame. The fix wheels the view back down before giving up,
and each choice in it was measured against emdash's xterm, so each is held here:

- only a TRUSTED wheel scrolls that xterm (`page.mouse.wheel`); a WheelEvent
  dispatched from the page is ignored, so a fix written that way does nothing;
- never with mouse reporting on (`enable-mouse-events`), where the wheel reaches
  claude as input;
- bounded, so the failure path cannot become a hang.

The sidecar needs a live emdash to exercise, so behaviour cannot be unit-tested here.
"""
import re
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp" / "emdash_control.mjs"


def _function(name: str) -> str:
    src = SIDECAR.read_text(encoding="utf-8")
    start = src.index(f"const {name} = ")
    end = src.index("\n};\n", start)
    return src[start:end]


def test_a_composer_read_scrolls_the_pane_down_before_giving_up():
    settled = _function("readComposerSettled")
    assert "scrollTermToBottom()" in settled
    assert "SCROLL_BUDGET_MS" in settled


def test_the_scroll_is_a_trusted_wheel_and_never_into_mouse_reporting():
    scroll = _function("scrollTermToBottom")
    assert "page.mouse.wheel(" in scroll
    assert not re.search(r"dispatchEvent\(\s*new WheelEvent", scroll)
    assert "enable-mouse-events" in scroll
    assert "SCROLL_MAX_WHEELS" in scroll
