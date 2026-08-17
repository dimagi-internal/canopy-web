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

import time

from .records import read_tail_records

ASK_TOOL = "AskUserQuestion"


def stamp_observed(menu: dict | None, *, now=None) -> dict | None:
    """Mark WHEN this dialog was seen. Every producer stamps; nothing else does.

    Without it a menu is undatable, and a client cannot tell one seen three
    seconds ago from one seen forty minutes ago — so the only way to discover a
    stale dialog is to tap it and be refused. That is the discovery path this
    whole surface exists to remove.

    Epoch seconds rather than an ISO string: the reader is arithmetic ("how old
    is this?"), the producers are a Django-free library and a runner, and there
    is no timezone in the question.
    """
    if menu is None:
        return None
    return {**menu, "observed_at": (now if now is not None else time.time())}

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


def _one_question(raw, index: int) -> dict | None:
    """One entry of `questions[]` -> a question dict, or None if unusable."""
    if not isinstance(raw, dict):
        return None

    options = []
    raw_options = raw.get("options")
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

    question = raw.get("question")
    question = question.strip() if isinstance(question, str) else ""
    header = raw.get("header")
    header = header.strip() if isinstance(header, str) else ""
    if not question:
        # A dialog with no question still has real options; the header is what
        # the TUI puts above them. Better a menu labelled by its header than no
        # menu at all — but never an unlabelled one.
        question = header
    if not question:
        return None

    return {
        "index": index,
        "question": question,
        "header": header,
        # The whole reason this file changed. The TUI draws a multi-select as
        # CHECKBOXES and a number key TOGGLES one instead of answering — so a
        # client that cannot see this flag renders "pick one" buttons for a
        # "pick any" question, and the runner presses a key that selects
        # nothing. Verified against a live TUI capture; see
        # `canopy_runner/tests/test_menu.py`.
        "multi_select": bool(raw.get("multiSelect")),
        "options": options,
    }


def _menu_from_input(payload) -> dict | None:
    """One `AskUserQuestion` tool input -> the menu dict, or None if unusable.

    Fails closed exactly like `find_menu`: no options means nothing to press,
    and a phone told an agent is blocked when it is working is a signal nobody
    trusts twice.

    Carries EVERY question, not just the first. The TUI shows them as tabs and
    will not submit until each has an answer, so a surface that renders only
    `questions[0]` cannot complete the ask no matter which button you press —
    it was structurally unable to, which is the bug this shape fixes. The
    top-level `question`/`title`/`options` stay pinned to the first question so
    an older client renders exactly what it rendered before.
    """
    if not isinstance(payload, dict):
        return None
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None

    questions = []
    for raw in raw_questions:
        parsed = _one_question(raw, len(questions))
        if parsed is None:
            # One malformed question must not cost the whole dialog its buttons,
            # but it MUST cost the structured path: submitting a set of answers
            # positionally against a list with a hole in it would answer the
            # wrong tab. Fall back to first-question-only rendering.
            return _legacy_menu(raw_questions)
        questions.append(parsed)

    first = questions[0]
    remaining = len(questions) - 1
    body = (f"{remaining} more question{'s' if remaining != 1 else ''} after this one."
            if remaining > 0 else "")

    return {
        "question": first["question"],
        "title": first["header"],
        "body": body,
        # A transcript cannot see which row the cursor is on — that is a
        # property of the rendered screen, not of the tool call.
        "selected": _EMPTY_SELECTED,
        "options": first["options"],
        "questions": questions,
        # Which half found it. The client must be able to ignore this; it exists
        # so an operator can tell the transcript path from the screen read.
        "source": "transcript",
    }


def _legacy_menu(raw_questions) -> dict | None:
    """First-question-only menu, for an ask this module cannot fully model.

    No `questions` key, deliberately: its absence is what tells a client to fall
    back to single-question rendering rather than trust a partial list.
    """
    first = _one_question(raw_questions[0] if raw_questions else None, 0)
    if first is None:
        return None
    remaining = len(raw_questions) - 1
    return {
        "question": first["question"],
        "title": first["header"],
        "body": (f"{remaining} more question{'s' if remaining != 1 else ''} after this one."
                 if remaining > 0 else ""),
        "selected": _EMPTY_SELECTED,
        "options": first["options"],
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
    return stamp_observed(menu)


# What Claude Code calls the hook that means "I want a human". It carries a
# message and nothing else — no options, no command, no way to reply.
NOTIFY_EVENT = "Notification"


def marker_from_hook(payload) -> dict | None:
    """A `Notification` hook -> a menu-shaped record with NO options, or None.

    **Why an option-less menu is worth having.** `AskUserQuestion` is a tool call
    and so arrives whole. The other things that stop an agent — a permission
    prompt, a trust gate — are drawn by Claude Code with no tool call behind
    them, so the only place they exist is the rendered terminal, and reading that
    means driving CDP, which CLICKS the task and steals focus (#510 was reverted
    for exactly that). The result was that those dialogs reached a phone only if
    somebody happened to be watching the session at the instant they appeared.

    A `Notification` needs neither: it says a human is wanted, and its `message`
    says roughly why. That is strictly more than nothing, and it is honest about
    being less than a menu — the client renders the words with no buttons and
    points at emdash.

    It is deliberately NOT parsed to work out which kind of dialog it is. The
    message is passed through as written, because guessing at its wording is how
    a signal starts lying, and the human reading it can tell.

    **Escape still works on it.** The runner re-reads the real screen before
    pressing anything, and a permission prompt parses there — so refusing is a
    genuine action even when no options could be listed here.
    """
    if not isinstance(payload, dict) or payload.get("hook_event_name") != NOTIFY_EVENT:
        return None
    message = payload.get("message")
    message = message.strip() if isinstance(message, str) else ""
    return stamp_observed({
        "question": message or "This session is waiting on you.",
        "title": "Waiting on you",
        "body": "",
        "selected": None,
        "options": [],
        "source": "notification",
    })


# Record types that are bookkeeping around a turn rather than a part of one:
# `system` carries the turn_duration/meta rows Claude Code appends AFTER the last
# message, and `queue-operation` records a queued prompt that has not been sent.
# Skipping them is what lets "how did the last turn end?" look at the last thing
# the conversation actually said.
_NON_CONVERSATIONAL = {"system", "queue-operation", "summary"}


def turn_ended_in_api_error(payload) -> bool:
    """Whether this session's turn already ended in an API error.

    **Why this is needed at all.** The runner tells a real "an agent is asking
    you something" `Notification` apart from a merely-idle one by turn STATE: a
    real one arrives between `UserPromptSubmit` and `Stop`. That discriminator
    assumes every turn ends with `Stop` — and one kind does not. When the API
    answers 500, Claude Code writes the error as an assistant message, appends
    its `turn_duration` row and returns to the prompt **without firing `Stop`**,
    so the session stays marked in-turn forever. Sixty seconds later the ordinary
    idle notification arrives, is read as a mid-turn block, and the chat surface
    locks itself behind a dialog that does not exist.

    Measured 2026-08-17 on `ace`'s `spark`: a 500 at 23:08:15Z, an idle
    `Notification` at 23:09:15Z, and a "Waiting on you" nobody could answer or
    type past — because there was nothing on the screen to answer.

    So the transcript is consulted for the one fact no hook reports: did the last
    thing the conversation said turn out to be an API error? `isApiErrorMessage`
    is written by Claude Code on exactly that record.

    **Fails closed** — an unreadable, absent or lagging transcript answers False,
    which is today's behaviour: a false "blocked" is the cost, and it is the one
    this whole path was built to accept in exchange for never missing a real ask.
    """
    if not isinstance(payload, dict):
        return False
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return False
    for record in reversed(read_tail_records(path)):
        if record.get("type") in _NON_CONVERSATIONAL or record.get("isSidechain"):
            continue
        return bool(record.get("isApiErrorMessage"))
    return False


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
            return stamp_observed(menu)
    return None
