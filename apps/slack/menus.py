"""A blocked agent's question, as Slack text — and a Slack reply, as an answer.

The menu is the one dict every surface shares (`canopy_transcript.questions`):
`question`/`options` for the first question, and `questions` (each with its own
`options` and `multi_select`) when the ask has several. Slack gets it as plain
numbered text answered by a plain reply, not buttons: buttons need the app's
interactivity turned on, and a number typed in the thread works everywhere a
message does — including on a phone mid-walk, which is when this matters.

An option-less menu (a `Notification` marker — canopy knows the agent wants a
human, not what for) is shown but NOT intercepted: canopy-web does not lock its
composer on one either, because nothing was parsed to say a dialog is really up.
"""
from __future__ import annotations

import hashlib
import json
import re

CANCEL_WORDS = {"cancel", "esc", "escape", "dismiss"}

_NUMBERS = re.compile(r"\d+(?:\s*(?:,|\s)\s*\d+)*")


def questions_of(menu: dict) -> list[dict]:
    """Every question in the ask, in order — a single-question menu included."""
    qs = menu.get("questions")
    if isinstance(qs, list) and qs:
        return qs
    return [{"question": menu.get("question") or "", "options": menu.get("options") or [],
             "multi_select": False}]


def answerable(menu: dict | None) -> bool:
    """Whether this menu has options a reply can pick — i.e. a real dialog."""
    return bool(menu) and any(q.get("options") for q in questions_of(menu))


def content_key(menu: dict) -> str:
    """Identity of the QUESTION, ignoring when it was seen."""
    shape = [
        [q.get("question") or "", [o.get("label") or "" for o in (q.get("options") or [])]]
        for q in questions_of(menu)
    ]
    return hashlib.sha256(json.dumps(shape).encode()).hexdigest()


def to_text(agent_slug: str, menu: dict, session_url: str = "") -> str:
    qs = questions_of(menu)
    lines = [f":raised_hand: *`{agent_slug}` is waiting on you*"]
    if not answerable(menu):
        lines.append(menu.get("question") or "It needs a person to look at it.")
        lines.append("canopy can't read the options from here — open the session to answer"
                     + (f": {session_url}" if session_url else "."))
        return "\n".join(lines)
    for i, q in enumerate(qs):
        header = f"*{i + 1}. {q.get('question')}*" if len(qs) > 1 else f"*{q.get('question')}*"
        if q.get("multi_select"):
            header += " _(pick any)_"
        lines.append(header)
        for o in q.get("options") or []:
            desc = f" — {o['description']}" if o.get("description") else ""
            lines.append(f"    `{o.get('number')}` {o.get('label')}{desc}")
    if len(qs) > 1:
        example = "; ".join("1" for _ in qs)
        how = f"Reply in this thread with one answer per question, separated by `;` (e.g. `{example}`)"
    elif qs[0].get("multi_select"):
        how = "Reply in this thread with the numbers you pick (e.g. `1,3`)"
    else:
        how = "Reply in this thread with a number"
    lines.append(f"{how}, or `cancel`.")
    return "\n".join(lines)


CANCEL = "cancel"


def parse_answer(text: str, menu: dict):
    """A reply -> `CANCEL`, or one list of chosen numbers per question, or None.

    None means "that is not an answer": the caller says how to answer rather than
    guessing. Strict on purpose — a number the runner presses lands in a live
    dialog, and a misread reply is a wrong choice made on someone's behalf.
    """
    reply = (text or "").strip().rstrip(".").strip()
    if reply.lower() in CANCEL_WORDS:
        return CANCEL
    qs = questions_of(menu)
    parts = [p.strip() for p in reply.split(";")]
    if len(parts) != len(qs):
        return None
    selections: list[list[int]] = []
    for part, q in zip(parts, qs):
        if not _NUMBERS.fullmatch(part):
            return None
        picks = [int(n) for n in re.findall(r"\d+", part)]
        valid = {o.get("number") for o in (q.get("options") or [])}
        if not picks or any(p not in valid for p in picks) or len(set(picks)) != len(picks):
            return None
        if not q.get("multi_select") and len(picks) != 1:
            return None
        selections.append(picks)
    return selections


def describe(selections, menu: dict) -> str:
    """"Proceed to Phase 4" — what was chosen, in the words the agent offered."""
    labels = []
    for picks, q in zip(selections, questions_of(menu)):
        by_number = {o.get("number"): o.get("label") for o in (q.get("options") or [])}
        labels.append(", ".join(str(by_number.get(p, p)) for p in picks))
    return "; ".join(labels)
