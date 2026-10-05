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
    assert "readComposer()" in body and "isEmpty(after.typed)" in body
