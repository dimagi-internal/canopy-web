"""The CDP sidecar must stay occlusion-proof — a STATIC guard on emdash_control.mjs.

The sidecar's header promises it "works while emdash is backgrounded (no foreground
focus needed)", and the whole file is written to keep that promise: every click goes
through `page.evaluate(... el.click())` rather than Playwright's `locator.click()`.

The difference is not stylistic. `locator.click()` first waits for the element to be
visible, enabled and STABLE, and "stable" means its bounding box was unchanged across
two ANIMATION FRAMES. macOS renders one login session at a time, so while the display
belongs to the other macOS account this window paints nothing, no frames arrive, and
the call hangs for its full timeout — while CDP answers perfectly, so nothing else
looks wrong. Separate debug ports isolate the two runners from each other; the SCREEN
is machine-wide and cannot be split.

One such call survived in the `create` path and cost two turns on 2026-08-10, ~6
minutes after the other account logged in; each failure wrote `~/.canopy/not-ready`
and took the box out of routing. It had been in the file the whole time and fired
three times in two weeks, because it only bites when a turn starts while the display
is elsewhere — rare enough to look like a fluke, frequent enough to keep happening.

The sidecar needs a live emdash to exercise (see test_cdp_control.py), so behaviour
here cannot be unit-tested. Reading the source can still hold the line.
"""
import re
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp" / "emdash_control.mjs"


def _code_lines():
    """Source with comment-only lines dropped — the rule is about calls, and the
    comments explaining the rule necessarily name the thing they forbid."""
    return [ln for ln in SIDECAR.read_text().splitlines()
            if not ln.lstrip().startswith("//")]


def test_sidecar_makes_no_actionability_gated_calls():
    """No `page.locator(...)`. Its click/fill/press all gate on rendering."""
    offenders = [ln.strip() for ln in _code_lines() if "page.locator(" in ln]
    assert not offenders, (
        "emdash_control.mjs must not use Playwright locators — they gate on the window "
        "painting, which it does not while the other macOS account holds the display. "
        "Dispatch the click/focus inside page.evaluate instead:\n  "
        + "\n  ".join(offenders))


def test_sidecar_waits_poll_on_a_timer_not_animation_frames():
    """`waitForFunction` defaults to `polling: 'raf'`, which is the same dependency by
    another name: a wait that never ticks while the window is backgrounded. Every call
    must name an explicit numeric polling interval."""
    for ln in _code_lines():
        if "waitForFunction(" not in ln:
            continue
        # The call spans lines; check the whole statement it opens.
        src = SIDECAR.read_text()
        for call in re.findall(r"waitForFunction\((?:[^()]|\([^()]*\))*\)", src, re.S):
            assert re.search(r"polling:\s*\d", call), (
                "waitForFunction must pass a numeric `polling` — the default 'raf' "
                f"stalls while emdash is backgrounded:\n{call}")
        break
