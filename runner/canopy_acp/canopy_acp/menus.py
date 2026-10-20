"""ACP requests -> the same menu shape the terminal parser produces.

This is the convergence point for "an agent is waiting on a human". Two
producers, one payload:

| | laptop (emdash owns the session) | cloud (canopy drives the agent) |
|---|---|---|
| source | the rendered terminal, over CDP | `session/request_permission` |
| extraction | parse a character grid | read a field |
| answering | press keys into a TUI | reply to a JSON-RPC request |

The CDP path exists only because emdash owns the local session and the dialog
is drawn, not sent. Everything that makes it delicate is absent here: no
half-drawn frames, no wrapped commands to rejoin, no guessing which terminal
pane is in view, and no chance of typing a digit into a shell. ACP hands over
`optionId`s and takes one back.

So the runner should build THIS dict on both paths and the client should never
learn which produced it — the same rule the live/durable split already follows.
When emdash exposes its own ACP stream the CDP parser is deleted and nothing
downstream changes.
"""
from __future__ import annotations

import re

# ACP option kinds, most-permissive first. Used only for ordering a display; the
# answer is always an explicit optionId the agent gave us.
_ALLOW_KINDS = ("allow_always", "allow_once")
_REJECT_KINDS = ("reject_always", "reject_once")


def menu_from_permission_request(params: dict) -> dict | None:
    """`session/request_permission` params -> the canopy menu payload.

    Shape captured live from claude-agent-acp 0.63.0:

        {"options": [{"kind": "allow_always", "name": "Always Allow Bash(rm …)",
                      "optionId": "allow_always"}, …],
         "toolCall": {"toolCallId": "toolu_01…", "rawInput": {"command": "rm …"},
                      "title": "rm …", "kind": "execute"}}

    `option_id` is carried per option because ACP answers by ID, not by
    position — the numbers exist only so one client can render both paths.
    """
    if not isinstance(params, dict):
        return None
    raw_options = params.get("options")
    if not isinstance(raw_options, list) or not raw_options:
        return None

    options = []
    for index, option in enumerate(raw_options, start=1):
        if not isinstance(option, dict):
            continue
        label = option.get("name") or option.get("kind") or f"Option {index}"
        options.append({
            "number": index,
            "label": str(label),
            "option_id": option.get("optionId"),
            "kind": option.get("kind") or "",
        })
    if not options:
        return None

    tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
    title = str(tool_call.get("title") or "")
    kind = str(tool_call.get("kind") or "")
    body = _command_of(tool_call)

    return {
        # ACP asks by presenting options rather than by phrasing a question, so
        # this mirrors the wording the TUI uses for the same decision.
        "question": "Do you want to proceed?",
        "title": _subject(kind, title),
        "body": body,
        "selected": None,
        "options": options,
        # Answering an ACP menu means replying to the request that is still open,
        # so the runner has to know which one.
        "tool_call_id": tool_call.get("toolCallId") or "",
    }


def _subject(kind: str, title: str) -> str:
    """A short label for what is being asked about.

    The TUI writes "Bash command"; ACP gives a `kind` (`execute`, `read`, `edit`)
    and the agent's own title. Prefer a shape the phone can read at a glance.
    """
    if kind == "execute":
        return "Command"
    if kind in ("read", "edit"):
        return f"File {kind}"
    return title[:60] or (kind.title() if kind else "")


def _command_of(tool_call: dict) -> str:
    """The thing being approved, as text.

    The command is the whole decision — a menu without it is unanswerable away
    from the keyboard, which is why the terminal parser goes to the trouble of
    rejoining a wrapped one. Here it arrives intact.
    """
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        for key in ("command", "file_path", "path", "pattern", "url"):
            value = raw_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
    title = tool_call.get("title")
    return title if isinstance(title, str) else ""


#: claude-agent-acp names a form's fields `question_<n>` (+ `question_<n>_custom`,
#: the per-question "Other" box) — `askUserQuestionsToCreateRequest`.
_QUESTION_FIELD = re.compile(r"question_(\d+)")


def menu_from_elicitation(params: dict) -> dict | None:
    """An `elicitation/create` for AskUserQuestion -> the canopy menu payload.

    The adapter builds this form FROM the tool input, so the form is turned back
    into that input and parsed by `canopy_transcript` — the very parser the
    laptop's hook uses. The two paths cannot drift because there is one parser.

    None for anything that is not an ask: an MCP server's own elicitation has its
    own fields, and buttons guessed for it would answer the wrong thing.
    """
    from canopy_transcript.questions import ASK_TOOL, menu_from_hook

    if not isinstance(params, dict) or params.get("mode", "form") != "form":
        return None
    schema = params.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return None
    fields = sorted(((int(m.group(1)), spec) for key, spec in properties.items()
                     if (m := _QUESTION_FIELD.fullmatch(key)) and isinstance(spec, dict)),
                    key=lambda pair: pair[0])
    if not fields or [i for i, _ in fields] != list(range(len(fields))):
        return None

    message = str(params.get("message") or "")
    questions = []
    for _, spec in fields:
        multi = spec.get("type") == "array"
        choices = (spec.get("items") or {}).get("anyOf") if multi else spec.get("oneOf")
        if not isinstance(choices, list) or not choices:
            return None
        questions.append({
            # One question's text rides `message`; with several, each field's
            # `description` carries its own.
            "question": spec.get("description") or (message if len(fields) == 1 else ""),
            "header": spec.get("title") or "",
            "multiSelect": multi,
            "options": [{"label": c.get("const") or c.get("title"),
                         "description": c.get("description") or ""}
                        for c in choices if isinstance(c, dict)],
        })
    menu = menu_from_hook({"hook_event_name": "PreToolUse", "tool_name": ASK_TOOL,
                           "tool_input": {"questions": questions}})
    if menu is None:
        return None
    menu["source"] = "acp"
    menu["tool_call_id"] = str(params.get("toolCallId") or "")
    return menu


def elicitation_answer(menu: dict, option: int | None, selections=None, texts=None) -> dict:
    """A person's answer -> the `elicitation/create` response the adapter folds
    back into AskUserQuestion's `answers`.

    `selections`/`texts` are what the web sends (one list of option numbers, and
    one optional typed answer, per question); `option` alone answers the first
    question, as an older client sends it. Nothing chosen and nothing typed is a
    refusal: `decline`, which the model reads as "the person skipped", rather than
    `cancel`, which aborts the tool call mid-turn.

    Raises ValueError for a number a question does not offer — an answer the agent
    never offered is not an answer, and the ask stays open.
    """
    questions = menu.get("questions") or [{"options": menu.get("options") or [],
                                           "multi_select": False}]
    if selections is None:
        selections = [[option]] if option is not None else []
    content: dict = {}
    for index, question in enumerate(questions):
        picks = selections[index] if index < len(selections) else []
        labels = []
        for number in picks or []:
            label = next((o["label"] for o in question.get("options") or []
                          if o.get("number") == number), None)
            if label is None:
                raise ValueError(f"option {number} is not offered by question {index + 1}")
            labels.append(label)
        if labels:
            content[f"question_{index}"] = labels if question.get("multi_select") else labels[0]
        typed = texts[index] if texts and index < len(texts) else None
        if isinstance(typed, str) and typed.strip():
            content[f"question_{index}_custom"] = typed.strip()
    if not content:
        return {"action": "decline"}
    return {"action": "accept", "content": content}


def option_id_for(menu: dict, number: int | None) -> str | None:
    """The ACP `optionId` a client's chosen NUMBER refers to.

    Returns None for a refusal, and raises for a number the menu does not
    offer — the same guard the keystroke path applies, for the same reason: an
    answer the agent never offered is not an answer, and the request stays open.
    """
    if number is None:
        return None
    for option in menu.get("options") or []:
        if option.get("number") == number:
            return option.get("option_id")
    raise ValueError(f"option {number} is not on this menu")
