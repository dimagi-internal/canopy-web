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
    for part in parts:
        if not _NUMBERS.fullmatch(part):
            return None
        selections.append([int(n) for n in re.findall(r"\d+", part)])
    return selections if valid(selections, menu) else None


def valid(selections, menu: dict) -> bool:
    """Whether this is a complete, legal answer: one pick-list per question, every
    number offered, no repeats, and exactly one for a pick-one question."""
    qs = questions_of(menu)
    if not isinstance(selections, list) or len(selections) != len(qs):
        return False
    for picks, q in zip(selections, qs):
        offered = {o.get("number") for o in (q.get("options") or [])}
        if not picks or any(p not in offered for p in picks) or len(set(picks)) != len(picks):
            return False
        if not q.get("multi_select") and len(picks) != 1:
            return False
    return True


# --- Block Kit: the same question as buttons -------------------------------
#
# A click comes back to /api/slack/interactions carrying `value` (for a button)
# and the message's current checkbox/radio `state`. Every value names the
# session and the question's content key, so a click on a question that has
# since changed or closed is recognised as stale instead of pressing a number
# into whatever the agent is showing NOW.

PICK, SUBMIT, DISMISS = "menu_pick", "menu_submit", "menu_dismiss"
#: Slack's limits: 75 chars of option text, 25 elements in an actions block.
_TEXT_MAX, _ELEMENTS_MAX = 75, 25


def _short(text: str, limit: int = _TEXT_MAX) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def button_value(session_id, menu: dict, selections=None) -> str:
    body = {"s": str(session_id), "k": content_key(menu)}
    if selections is not None:
        body["sel"] = selections
    return json.dumps(body, separators=(",", ":"))


def _one_tap(qs: list[dict]) -> bool:
    """A single pick-one question: a button per option answers in one tap."""
    return len(qs) == 1 and not qs[0].get("multi_select") \
        and len(qs[0].get("options") or []) <= _ELEMENTS_MAX - 1


def to_blocks(agent_slug: str, menu: dict, session_id) -> list[dict]:
    """Interactive blocks for an ANSWERABLE menu (see `answerable`)."""
    qs = questions_of(menu)
    blocks: list[dict] = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": f":raised_hand: *`{agent_slug}` is waiting on you*"},
    }]
    for i, q in enumerate(qs):
        lines = [f"*{q.get('question')}*" + ("  _(pick any)_" if q.get("multi_select") else "")]
        lines += [f"`{o.get('number')}` {o.get('label')}" + (f" — {o['description']}" if o.get("description") else "")
                  for o in (q.get("options") or [])]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}})
        if _one_tap(qs):
            continue
        options = [{"text": {"type": "plain_text", "text": _short(f"{o.get('number')}. {o.get('label')}")},
                    "value": str(o.get("number"))} for o in (q.get("options") or [])[:10]]
        element = {"type": "checkboxes" if q.get("multi_select") else "radio_buttons",
                   "action_id": f"q{i}", "options": options}
        blocks.append({"type": "actions", "block_id": f"menu_q{i}", "elements": [element]})

    dismiss = {"type": "button", "action_id": DISMISS, "text": {"type": "plain_text", "text": "Dismiss"},
               "value": button_value(session_id, menu)}
    if _one_tap(qs):
        buttons = [{"type": "button", "action_id": f"{PICK}_{o.get('number')}",
                    "text": {"type": "plain_text", "text": _short(o.get("label"))},
                    "value": button_value(session_id, menu, [[o.get("number")]])}
                   for o in qs[0].get("options") or []]
        if buttons:
            buttons[0]["style"] = "primary"
        blocks.append({"type": "actions", "block_id": "menu_answer", "elements": buttons + [dismiss]})
    else:
        submit = {"type": "button", "action_id": SUBMIT, "style": "primary",
                  "text": {"type": "plain_text", "text": "Submit"}, "value": button_value(session_id, menu)}
        blocks.append({"type": "actions", "block_id": "menu_answer", "elements": [submit, dismiss]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": "Or reply in this thread with the number" + ("s" if len(qs) > 1 else "") + ", or `cancel`."}]})
    return blocks


def selections_from_state(state: dict, menu: dict):
    """The checkbox/radio choices a Submit carried, as one pick-list per question."""
    values = (state or {}).get("values") or {}
    out = []
    for i, _q in enumerate(questions_of(menu)):
        element = (values.get(f"menu_q{i}") or {}).get(f"q{i}") or {}
        chosen = element.get("selected_options")
        if chosen is None:
            chosen = [element["selected_option"]] if element.get("selected_option") else []
        try:
            out.append([int(o["value"]) for o in chosen])
        except (KeyError, TypeError, ValueError):
            return None
    return out


def resolved_blocks(question: str, outcome: str) -> list[dict]:
    """What a question post becomes once it is answered or gone: no buttons left."""
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{question}*"[:3000]}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome[:3000]}]}]


def describe(selections, menu: dict) -> str:
    """"Proceed to Phase 4" — what was chosen, in the words the agent offered."""
    labels = []
    for picks, q in zip(selections, questions_of(menu)):
        by_number = {o.get("number"): o.get("label") for o in (q.get("options") or [])}
        labels.append(", ".join(str(by_number.get(p, p)) for p in picks))
    return "; ".join(labels)
