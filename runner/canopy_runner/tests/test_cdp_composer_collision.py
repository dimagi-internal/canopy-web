"""A human's draft the first composer read missed must still never be sent.

Turn 64cada76 (2026-10-23, failure investigation 2): Jonathan had "okay great, let"
typed in ACE's composer. open-send's read before typing came back empty, so there
was no collision dialog; the chat message was typed onto the end of his draft, the
post-Enter recheck saw "not ours" and stopped quietly, and the sidecar reported
`sent`. Recovery then found `composer_holds_other_text` and the turn failed
undelivered. The same read finds the draft today, so what failed was the frame,
not the rule.

open-send now looks again right before typing, and once more after typing and
BEFORE Enter (`composer_check.mjs`). A draft in front of our message turns the send
into a `collision`: our text is taken back off the end and the human is asked.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from canopy_runner import execute

CDP = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp"
MODULE = CDP / "composer_check.mjs"
SIDECAR = CDP / "emdash_control.mjs"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

MSG = ("are those issues or did you fix them? go ahead and fix and merge them, and also "
       "tell me what is left")


def _call(fn, *args):
    script = (
        f"const m = await import({json.dumps(MODULE.as_uri())});"
        f"process.stdout.write(JSON.stringify(m.{fn}(...{json.dumps(list(args))})));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


# ── the rule ────────────────────────────────────────────────────────────────────

@needs_node
def test_the_incident_line_is_a_foreign_lead():
    assert _call("hasForeignLead", MSG, "okay great, let " + MSG) is True


@needs_node
@pytest.mark.parametrize("typed", [
    MSG,                                            # our message, alone
    MSG[:40],                                       # still arriving
    "are those issues or did you fix │ them? go ahead",   # wrapped and framed
    "[Pasted text #1 +3 lines]",                    # collapsed multi-line paste
    "[Pasted text #1] tell me what is left",        # long one-liner, split
    "and merge them, and also tell me what is left",      # only the tail fits the pane
    "",
])
def test_our_own_message_is_not_foreign(typed):
    assert _call("hasForeignLead", MSG, typed) is False


@needs_node
def test_a_lone_key_binding_is_compared_as_typed():
    # "?" is typed as " ?" (typed_text.mjs); squashing makes them the same.
    assert _call("hasForeignLead", "?", " ?") is False
    assert _call("hasForeignLead", "?", "wait ?") is True


@needs_node
def test_the_draft_left_after_taking_ours_back():
    assert _call("draftWithoutOurs", MSG, "okay great, let " + MSG) == "okaygreat,let"


@needs_node
@pytest.mark.parametrize("typed", [
    MSG,                                            # nothing of theirs to keep
    "okay great, let [Pasted text #1 +3 lines]",    # ours collapsed: can't aim at it
    "okay great, let " + MSG + " more",             # ours is not at the end
])
def test_nothing_is_deleted_unless_our_text_ends_the_line(typed):
    assert _call("draftWithoutOurs", MSG, typed) is None


# ── the wiring (needs a live emdash to exercise, so static guards) ──────────────

def _open_send_branch() -> str:
    src = SIDECAR.read_text(encoding="utf-8")
    start = src.index("command === 'open-send'")
    end = src.index("command === 'interrupt'", start)
    return "\n".join(ln for ln in src[start:end].splitlines() if not ln.lstrip().startswith("//"))


def _submit() -> str:
    branch = _open_send_branch()
    return branch[branch.index("const submit = async"):branch.index("if (clearFirst)")]


def test_composer_is_reread_right_before_typing():
    body = _submit()
    assert body.index("readComposer()") < body.index("insertText(")
    assert "return { line: now.typed, when: 'before-type' }" in body


def test_a_foreign_lead_is_checked_after_typing_and_before_enter():
    body = _submit()
    typed_at = body.index("insertText(")
    check = body.index("hasForeignLead(body, typed.typed)")
    assert typed_at < check < body.index("press('Enter')")
    assert "takeBackOurs(body, typed.typed)" in body


def test_a_late_collision_is_reported_as_a_collision_not_sent():
    branch = _open_send_branch()
    assert "const late = await submit(text, true);" in branch
    tail = branch[branch.index("const late = await submit(text, true);"):]
    assert tail.index("action: 'collision'") < tail.index("action: 'sent'")


def test_clear_and_send_is_not_guarded():
    """There the human already chose to discard the line."""
    assert "await submit(text, false);\n      out({ ok: true, action: 'sent-cleared'" in _open_send_branch()


def test_take_back_stops_at_the_humans_text():
    branch = _open_send_branch()
    fn = branch[branch.index("const takeBackOurs"):branch.index("const submit = async")]
    assert "draftWithoutOurs(body, typed)" in fn
    assert "!line.startsWith(want)" in fn


# ── the evidence ────────────────────────────────────────────────────────────────

def test_the_pre_type_read_rides_along_on_the_snapshot():
    sent = {"action": "sent", "pre": {"found": True, "typed": ""}}
    assert execute._pre_send_fields(sent) == {"pre_found": True, "pre_line": ""}
    assert execute._pre_send_fields({"action": "sent"}) == {}
    assert execute._pre_send_fields(None) == {}


def test_a_late_collision_says_which_look_caught_it():
    res = {"action": "collision", "line": "okay great, let", "when": "after-type",
           "restored": True, "pre": {"found": True, "typed": ""}}
    assert execute._late_collision_fields(res) == {
        "when": "after-type", "restored": True, "pre_found": True, "pre_line": ""}
    assert execute._late_collision_fields({"action": "collision", "line": "x"}) == {}
