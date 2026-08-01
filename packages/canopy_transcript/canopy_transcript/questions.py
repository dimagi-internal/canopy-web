"""The dialog a blocked agent is waiting on — from its hook, or its transcript.

**Read this first: the transcript is the WEAKER half, and the section below is
kept only because it explains the design.** `pending_question` cannot see a live
dialog. Claude Code writes the `AskUserQuestion` `tool_use` record when the ask
is ANSWERED, not when it is made — measured 2026-08-01 across 60 transcripts on
a live box: 39 such records, every one already answered, zero pending, while two
sessions sat blocked on visible dialogs with nothing in their files. So it
reports dialogs that are over and never one that is waiting, and it fails
silently, because `None` also means "nothing is blocked".

`menu_from_hook` is the primary path: `PreToolUse` fires when the call STARTS and
carries the same `tool_input`, so every property argued for below holds — and it
arrives at the only moment that is useful. `pending_question` is consulted second
(it still wins on a session whose hooks were never installed) and would become
correct on its own the day Claude Code flushes that record eagerly.

**Why the transcript at all.** The menu used to exist in exactly one place —
the rendered terminal — so seeing it meant driving emdash over CDP, and CDP's
`openTask` CLICKS the task and steals focus. #495 wired that onto the
`Notification` hook; #510 had to rip it back out (it yanked emdash to whatever
agent had just asked, mid-typing) and left the read to be triggered "on demand".
Nothing ever triggered it, so from 2026-07-28 a menu could reach a phone only by
a chat send failing against it. Meanwhile `ace`'s `spark` session sat blocked on
an `AskUserQuestion` for 52 minutes with the phone showing nothing.

But the fleet's dialog is not a permission prompt. Sessions run
`⏵⏵ bypass permissions on`, so what actually blocks an agent is
**AskUserQuestion** — and that is a TOOL CALL, which means the question, every
option and every option's description are already sitting in the transcript the
runner tails anyway. No CDP, no focus, no screen parsing, no round trip.

That difference is what makes the signal usable rather than merely available:
it can be computed for every session on the box on the ordinary report cadence,
whether or not anybody is watching, and recomputed whenever someone opens a
session — so a menu can no longer be missed by not being connected when it
appeared.

**The screen reader stays** (`canopy_runner.menu`) for the dialogs a transcript
genuinely cannot see: a real permission prompt, a trust gate, anything Claude
Code draws without a tool call behind it. Both emit the SAME dict, so a client
never learns which produced it.

**Numbering is the load-bearing part**, because an answer is delivered as a
keystroke. Claude Code renders the declared options first, numbered from 1 in
declared order, and then appends its own ("Type something", "Chat about this")
— verified against a live capture (`canopy_runner/tests/test_menu.py`). So the
declared options' numbers are safe to derive here. We do NOT invent the appended
ones. Nothing rests on that alone: the runner re-reads the real screen and
refuses any option that is not on it, so a divergence costs a dropped tap, never
a wrong keypress.
"""
from __future__ import annotations

ASK_TOOL = "AskUserQuestion"

# The menu the client renders is one shape whichever half produced it. Keep this
# in lockstep with `canopy_runner.hooks.read_hook_menu_from`.
_EMPTY_SELECTED = None


def _blocks(record):
    """The content blocks of a record, or () for anything malformed.

    Deliberately total: this runs for every open session on the box on a ~10s
    cadence, and a crash here would take the liveness report down with it.
    """
    if not isinstance(record, dict):
        return ()
    message = record.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    return tuple(b for b in content if isinstance(b, dict)) if isinstance(content, list) else ()


def _menu_from_input(payload) -> dict | None:
    """One `AskUserQuestion` tool input -> the menu dict, or None if unusable.

    Fails closed exactly like `find_menu`: no options means nothing to press,
    and a phone told an agent is blocked when it is working is a signal nobody
    trusts twice.
    """
    if not isinstance(payload, dict):
        return None
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        return None
    first = questions[0]
    if not isinstance(first, dict):
        return None

    options = []
    raw_options = first.get("options")
    if not isinstance(raw_options, list):
        return None
    for opt in raw_options:
        if not isinstance(opt, dict):
            continue
        label = opt.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        description = opt.get("description")
        options.append({
            "number": len(options) + 1,
            "label": label.strip(),
            "description": description.strip() if isinstance(description, str) else "",
        })
    if not options:
        return None

    question = first.get("question")
    question = question.strip() if isinstance(question, str) else ""
    header = first.get("header")
    header = header.strip() if isinstance(header, str) else ""
    if not question:
        # A dialog with no question still has real options; the header is what
        # the TUI puts above them. Better a menu labelled by its header than no
        # menu at all — but never an unlabelled one.
        question = header
    if not question:
        return None

    # AskUserQuestion may carry several questions and the TUI shows them one at
    # a time. Say so rather than presenting a 1-of-3 dialog as the whole ask.
    remaining = len(questions) - 1
    body = f"{remaining} more question{'s' if remaining != 1 else ''} after this one." if remaining > 0 else ""

    return {
        "question": question,
        "title": header,
        "body": body,
        # A transcript cannot see which row the cursor is on — that is a
        # property of the rendered screen, not of the tool call.
        "selected": _EMPTY_SELECTED,
        "options": options,
        # Which half found it. The client must be able to ignore this; it exists
        # so an operator can tell the transcript path from the screen read.
        "source": "transcript",
    }


def menu_from_hook(payload) -> dict | None:
    """A `PreToolUse` hook payload for `AskUserQuestion` -> the menu, or None.

    **Why this exists, when the transcript already parses the same tool input.**
    It does not get the chance. Claude Code writes the `tool_use` record for a
    dialog only once the dialog is ANSWERED — measured 2026-08-01 across 60
    transcripts on a live box: 39 `AskUserQuestion` records, every one of them
    already answered, and zero pending, while two sessions sat visibly blocked
    with nothing in their files at all. So `pending_question` can report a
    question that is over and never one that is waiting, which is the exact
    inverse of the job.

    A hook has no such lag: `PreToolUse` fires when the call STARTS, and its
    `tool_input` is the same object the transcript would eventually carry. Same
    parse, same dict, ~0 cost, no CDP and no stolen focus — it just arrives at
    the only moment that is useful.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name") != "PreToolUse":
        return None
    if payload.get("tool_name") != ASK_TOOL:
        return None
    menu = _menu_from_input(payload.get("tool_input"))
    if menu is not None:
        menu["source"] = "hook"
    return menu


def hook_retires_menu(payload) -> bool:
    """Whether this hook event means any dialog on that session is gone.

    The answer arriving (`PostToolUse` for the ask) is the obvious one. The turn
    ending and a new prompt being submitted matter just as much: a human who
    answered at the laptop leaves no `PostToolUse` on OUR listener if the hook
    missed it, and a stale menu is worse than no menu — its numbers get typed
    into an ordinary prompt, where the agent reads a bare "2" as an instruction.
    """
    if not isinstance(payload, dict):
        return False
    event = payload.get("hook_event_name")
    if event in ("Stop", "UserPromptSubmit"):
        return True
    return event in ("PostToolUse", "PostToolUseFailure") and payload.get("tool_name") == ASK_TOOL


def pending_question(records) -> dict | None:
    """The `AskUserQuestion` this session is waiting on, or None.

    "Waiting" means asked and not yet answered: a `tool_use` block for
    `AskUserQuestion` with no later `tool_result` carrying its id. Matching on
    the id matters — a result for some OTHER call says nothing about this one,
    and treating any result as the answer would clear a dialog still on screen.

    Sidechain (subagent) records are skipped: a Task agent's dialog is not on
    the screen a human would answer, and there is no way to route a keystroke
    into it.
    """
    asked: list[tuple[str, dict]] = []
    answered: set[str] = set()
    for record in records or ():
        if not isinstance(record, dict) or record.get("isSidechain"):
            continue
        for block in _blocks(record):
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") == ASK_TOOL:
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    asked.append((tool_use_id, block.get("input")))
            elif kind == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    answered.add(tool_use_id)

    # Newest first: with two asks in one session, the later one is what is on
    # screen, and pressing the earlier one's numbers would answer the wrong
    # dialog.
    for tool_use_id, payload in reversed(asked):
        if tool_use_id in answered:
            continue
        menu = _menu_from_input(payload)
        if menu is not None:
            return menu
    return None
