"""A multi-line message typed into a live session must actually be SUBMITTED.

Claude Code reads a burst of input containing a newline as a paste, and an Enter
arriving inside that burst becomes part of it — a newline in the composer, not a
submit. `insertText` followed at once by `press('Enter')` is that burst, so every
multi-line send (a Slack reply with an attachment note, a web chat with a
screenshot path) sat typed-but-unsent and failed as undelivered (turn fe0a0280,
2026-10-05). Reproduced on a raw pty against `claude`: text + Enter in one write
never submits; a gap of >= 50ms always does.

The sidecar needs a live emdash to exercise, so this is a static guard: in the
open-send branch no Enter may follow an insertText directly — both sends go through
`submit`, which waits for the paste to settle and re-presses a swallowed Enter.
"""
import re
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp" / "emdash_control.mjs"


def _open_send_branch() -> str:
    src = SIDECAR.read_text()
    start = src.index("command === 'open-send'")
    end = src.index("command === 'interrupt'", start)
    return "\n".join(ln for ln in src[start:end].splitlines() if not ln.lstrip().startswith("//"))


def test_no_enter_directly_after_insert_text_in_open_send():
    branch = _open_send_branch()
    assert not re.search(r"insertText\([^)]*\);\s*\n\s*await page\.keyboard\.press\('Enter'\)", branch)


def test_every_open_send_goes_through_submit():
    branch = _open_send_branch()
    assert branch.count("await submit(text)") == 2
    assert branch.count("insertText(") == 1          # the one inside submit


def test_submit_waits_and_rechecks_a_multiline_message():
    branch = _open_send_branch()
    body = branch[branch.index("const submit = async"):]
    assert "waitForTimeout(PASTE_SETTLE_MS)" in body.split("press('Enter')")[0]
    assert "readComposer()" in body and "stillOurs(body, after.typed)" in body


def _still_ours_src() -> str:
    branch = _open_send_branch()
    return branch[branch.index("const stillOurs"):branch.index("const submit = async")]


def test_a_multiline_body_is_rechecked_by_emptiness_not_text():
    # claude collapses a long paste to "[Pasted text #1 +N lines]", so the
    # multi-line recheck must not compare text.
    src = _still_ours_src()
    assert "if (isEmpty(typed)) return false;" in src
    assert "if (/\\n/.test(body)) return true;" in src


# ── 2026-10-07 (ACE): a single-line Enter swallowed, focus stolen mid-send ──────

def test_a_single_line_body_is_rechecked_too():
    """It used to press Enter once and return, so a swallowed Enter left a one-line
    message sitting in the composer. The recheck loop now runs for every body."""
    branch = _open_send_branch()
    body = branch[branch.index("const submit = async"):branch.index("if (clearFirst)")]
    assert "if (!/\\n/.test(body)) return;" not in body
    loop = body[body.index("press('Enter')"):]
    assert "readComposer()" in loop and "press('Enter')" in loop.split("readComposer()")[1]


def test_a_single_line_recheck_matches_by_text():
    """A slash command's first Enter can replace the text with an autocomplete pick;
    pressing again on THAT would send something never typed. So a one-liner is
    only re-submitted while the composer still holds exactly it."""
    src = _still_ours_src()
    assert "squash(typed) === squash(body)" in src


def test_focus_is_confirmed_immediately_before_every_keystroke_burst():
    """insertText and press go to whatever holds focus when they arrive; a human's
    click between focus and insert diverts the message. Every typing site
    re-asserts (and verifies) focus first."""
    branch = _open_send_branch()
    body = branch[branch.index("const submit = async"):]
    assert body.index("await ensureFocus(task);") < body.index("insertText(")
    assert "await ensureFocus(task);\n      await page.keyboard.press('Control+U');" in branch
    src = SIDECAR.read_text()
    focus = src[src.index("const focusTerminal"):src.index("const ensureFocus")]
    assert "document.activeElement === ta" in focus
    ensure = src[src.index("const ensureFocus"):src.index("const readComposer")]
    assert "FOCUS_LOST" in ensure
