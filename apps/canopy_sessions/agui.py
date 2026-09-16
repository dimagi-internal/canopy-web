"""canopy's session frames, projected into AG-UI.

**The whole module is a seam.** AG-UI types are constructed here and nowhere
else, and canopy's own frames are never replaced by them — this is a projection
alongside `WsEvent`, not a migration of it. The reason is stated plainly rather
than implied: `ag-ui-protocol` is `0.1.x`, its enum already carries removals
scheduled for 1.0 (`THINKING_*` → `REASONING_*`), and `canopy-ui` is published
to public npm at 0.7.0 with ace-web downstream. A wrong bet must cost one
module, not a protocol migration across two repos.

**Why AG-UI at all.** canopy invented this contract twice already — ACP turned
out to specify what the runner had hand-rolled, and AG-UI specifies what the
session socket had. The convergence goes further than the shape: AG-UI's
`Interrupt` carries `expires_at` and its resume carries `status="cancelled"`,
which are exactly the two corrections canopy paid for during the "clicking does
nothing" incident (a menu must carry its own staleness; a refusal must ride back
as a value, not a log line). A standard that independently reached canopy's
hard-won answers is a better place to keep them than a file only we read.

**What does not map, and why that is fine.** AG-UI models one user and one
agent. canopy's chat is multiplayer — a co-edited draft, a presence roster — and
its sessions are placed on a particular runner. Those ride `CUSTOM`, which is
one of three extension points AG-UI designs in on purpose (`CUSTOM`, the open
`activity_type` on `ACTIVITY_*`, and `metadata` on every event, where only the
`ag-ui` key is reserved and their own comment says "Every other key is user
space"). Extending is not working around it.

**Projection, not translation of meaning.** Every function here is pure: frames
in, events out, no ORM and no I/O, so the mapping is provable in a unit test
rather than asserted in a design doc. Skipping that proof is what let an earlier
attempt claim AG-UI adoption while shipping none of it.
"""

from __future__ import annotations

import json
from typing import Any

from ag_ui.core import events as E
from ag_ui.core import types as T

#: Prefix on every canopy-specific `CUSTOM` event. Namespaced so a client
#: reading a canopy stream can tell "the protocol says this" from "canopy says
#: this", and so a future AG-UI event with the same idea does not collide.
CUSTOM_PREFIX = "canopy."

#: Key under which canopy's own fields ride on an AG-UI event's `metadata`.
#:
#: AG-UI reserves only the `ag-ui` key and its own comment says "Every other key
#: is user space", so this is the extension point used as designed. It carries
#: the two things canopy needs that the protocol has no slot for:
#:
#:   turn_index — the transcript ordinal. It is how a live row sorts into the
#:                position it will occupy after a reload, and how a web send is
#:                told apart from the echo of the same text arriving from the
#:                runner. AG-UI orders by arrival, which is not the same thing.
#:   plaintext  — the settled text of a message that arrived WHOLE. An AG-UI
#:                client accumulates deltas and so never needs it; canopy's
#:                reducer dedupes a user message by comparing text, and cannot.
#:   block      — the runner's own tool payload, verbatim. `stream_map` passes
#:                it straight through from the transcript, so its shape is the
#:                PRODUCER's (Anthropic's `tool_use` block from the laptop, the
#:                cloud runner's from ACP) and canopy deliberately does not
#:                interpret it. AG-UI's tool call carries a name and an
#:                arguments string, which is the right thing for a third-party
#:                client and strictly less than the block holds — so the block
#:                rides alongside rather than being reconstructed on the way
#:                back, which would invent an `id` and a `type` that the real
#:                payload may not have had.
#:
#: Without these the projection is LOSSY, and a canopy client reading the AG-UI
#: stream would render a subtly worse conversation than one reading canopy's own
#: frames — which would make the projection a downgrade dressed as a standard.
METADATA_KEY = "canopy"

#: `ACTIVITY_SNAPSHOT.activity_type` for the agent's working/idle/blocked state.
#: An open string by design — this is the extension point, used as intended.
ACTIVITY_TYPE = "canopy.session"


def _tool_call_id(frame_data: dict) -> str:
    """The id AG-UI correlates a tool call by.

    canopy keys a tool row by `tool_message_id`, which is already unique per
    call and already what a reload sorts on, so it is reused rather than a
    second identifier being minted. Two id spaces for one call is how a
    `tool_call_update` ends up merging into nothing.
    """
    return str(frame_data.get("tool_message_id") or "")


def _meta(**fields: Any) -> dict | None:
    """canopy's own fields, namespaced, or None when there are none.

    None rather than an empty dict so `exclude_none` drops the key entirely: an
    event carrying `metadata: {"canopy": {}}` claims to say something about
    canopy and does not.
    """
    present = {k: v for k, v in fields.items() if v is not None}
    return {METADATA_KEY: present} if present else None


def _custom(name: str, value: Any) -> E.CustomEvent:
    return E.CustomEvent(type=E.EventType.CUSTOM, name=f"{CUSTOM_PREFIX}{name}", value=value)


def _interrupt_from_menu(menu: dict) -> T.Interrupt:
    """canopy's blocked-agent dialog as an AG-UI interrupt.

    The correspondence is unusually exact, and it is worth naming field by
    field because each of these cost canopy an incident:

      * `expires_at` ← `observed_at`. The dialog lives on a terminal; this
        object is a copy, and without an age the only way to discover the copy
        is stale is to tap it and be refused.
      * `response_schema` ← the option list. A dialog a client cannot render the
        right control for is a dialog nobody can answer: an `AskUserQuestion`
        draws checkboxes for a multi-select and a number key TOGGLES rather than
        answers, so a surface that cannot see that flag presses the wrong key.
      * `reason` distinguishes a parsed menu from a bare notification marker.
        A marker must not lock a composer — nothing was parsed to produce one,
        so "a dialog is up" is a guess, and a wrong guess has no way out.
    """
    questions = menu.get("questions") or []
    if not questions:
        # The single-question fields still describe question 1, which is what a
        # producer older than multi-question support sends.
        questions = [
            {
                "index": 0,
                "question": menu.get("question") or "",
                "options": menu.get("options") or [],
            }
        ]

    return T.Interrupt(
        id=str(menu.get("id") or menu.get("observed_at") or "menu"),
        # `source` says which producer saw it (transcript, hook, screen read).
        # Kept as the reason because "why is this run paused" is exactly that.
        reason=str(menu.get("source") or "ask_user_question"),
        message=menu.get("title") or menu.get("question") or "",
        response_schema={
            "type": "object",
            "properties": {
                f"q{q.get('index', i)}": {
                    "type": "array" if q.get("multi_select") else "string",
                    "title": q.get("question") or "",
                    "enum": [o.get("label") for o in (q.get("options") or [])],
                }
                for i, q in enumerate(questions)
            },
        },
        expires_at=_expires_at(menu),
        metadata={
            # The body is often the only thing that makes a dialog answerable
            # away from the keyboard: "Do you want to proceed?" says nothing
            # without the command it means.
            "canopy": {
                "body": menu.get("body") or "",
                "questions": questions,
                "answer_error": menu.get("answer_error") or "",
                "answer_note": menu.get("answer_note") or "",
                "restored": bool(menu.get("restored")),
            }
        },
    )


def _expires_at(menu: dict) -> str | None:
    """`observed_at` (epoch seconds) as an ISO-8601 string, or None.

    None where canopy does not know, rather than a far-future default: a menu
    that claims freshness it has not got is worse than one that admits it does
    not know, because the client stops warning about the case that matters.
    """
    observed = menu.get("observed_at")
    if not observed:
        return None
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(observed), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def project(frame: dict, *, thread_id: str, run_id: str = "") -> list[E.BaseEvent]:
    """One canopy `WsEvent` frame → zero or more AG-UI events.

    Zero is a real answer: some canopy frames carry no AG-UI meaning, and
    inventing one for them would put noise on a stream a third-party client
    reads. Unknown frames also return zero rather than raising — this projection
    must never be able to break the socket it rides on.
    """
    event = frame.get("event") or ""
    data = frame.get("data") or {}

    if event == "chat.stream_start":
        return [
            E.TextMessageStartEvent(
                type=E.EventType.TEXT_MESSAGE_START,
                message_id=str(data.get("message_id") or ""),
                role="assistant",
                metadata=_meta(turn_index=data.get("turn_index")),
            )
        ]

    if event == "chat.delta":
        text = data.get("text") or ""
        # AG-UI rejects an empty delta, and canopy has been known to emit one on
        # a keepalive. Dropping is correct: an empty chunk changes nothing.
        if not text:
            return []
        return [
            E.TextMessageContentEvent(
                type=E.EventType.TEXT_MESSAGE_CONTENT,
                message_id=str(data.get("message_id") or ""),
                delta=text,
            )
        ]

    if event == "chat.stream_complete":
        return [
            E.TextMessageEndEvent(
                type=E.EventType.TEXT_MESSAGE_END,
                message_id=str(data.get("message_id") or ""),
                metadata=_meta(plaintext=data.get("plaintext")),
            )
        ]

    if event == "chat.user_message":
        # A human typed into emdash rather than into this page. No client echoed
        # it, so without this frame it reaches a browser only on reload — and a
        # chunk is the right shape because it arrives whole, not streamed.
        return [
            E.TextMessageChunkEvent(
                type=E.EventType.TEXT_MESSAGE_CHUNK,
                message_id=str(data.get("message_id") or ""),
                role="user",
                delta=data.get("plaintext") or "",
                metadata=_meta(turn_index=data.get("turn_index")),
            )
        ]

    if event == "chat.tool_use":
        block = data.get("block") or {}
        call_id = _tool_call_id(data)
        args = block.get("input")
        return [
            E.ToolCallStartEvent(
                type=E.EventType.TOOL_CALL_START,
                tool_call_id=call_id,
                tool_call_name=str(block.get("name") or "tool"),
                parent_message_id=(str(data["parent_message_id"])
                                   if data.get("parent_message_id") else None),
                metadata=_meta(turn_index=data.get("turn_index"), block=block),
            ),
            # Arguments arrive whole from canopy, not streamed, so one ARGS
            # frame carries the lot. The JSON string is AG-UI's shape: a tool
            # call's arguments are a string there precisely because they may be
            # streamed in fragments that are not individually valid JSON.
            E.ToolCallArgsEvent(
                type=E.EventType.TOOL_CALL_ARGS,
                tool_call_id=call_id,
                delta=json.dumps(args if args is not None else {}),
            ),
            E.ToolCallEndEvent(type=E.EventType.TOOL_CALL_END, tool_call_id=call_id),
        ]

    if event == "chat.tool_result":
        block = data.get("block") or {}
        content = block.get("content")
        return [
            E.ToolCallResultEvent(
                type=E.EventType.TOOL_CALL_RESULT,
                message_id=_tool_call_id(data),
                tool_call_id=_tool_call_id(data),
                content=content if isinstance(content, str) else json.dumps(content),
                metadata=_meta(
                    turn_index=data.get("turn_index"),
                    parent_message_id=data.get("parent_message_id"),
                    block=block,
                ),
            )
        ]

    if event == "session.activity":
        state = data.get("state") or ""
        out: list[E.BaseEvent] = [
            # The open `activity_type` is the designed extension point, so the
            # agent's own working/idle/blocked reading rides it rather than
            # being squeezed into run lifecycle, which answers a different
            # question (is a RUN open) on a much slower clock.
            E.ActivitySnapshotEvent(
                type=E.EventType.ACTIVITY_SNAPSHOT,
                message_id=str(data.get("message_id") or thread_id),
                activity_type=ACTIVITY_TYPE,
                content={"state": state},
                replace=True,
            )
        ]
        if state == "working" and run_id:
            out.append(
                E.RunStartedEvent(
                    type=E.EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id
                )
            )
        return out

    if event == "session.menu":
        menu = data.get("menu")
        if not menu:
            # The retraction — somebody answered at the keyboard. There is no
            # "interrupt withdrawn" event, so it rides CUSTOM; a client that
            # ignores it simply keeps showing a dialog that will refuse, which
            # is the pre-existing behaviour and not a regression.
            return [_custom("menu.retracted", {"thread_id": thread_id})]
        if not run_id:
            # An interrupt outcome belongs to a RUN. Without one there is
            # nothing to attach it to, so it goes over CUSTOM rather than being
            # given a fabricated run id that no resume could ever match.
            return [_custom("menu", menu)]
        return [
            E.RunFinishedEvent(
                type=E.EventType.RUN_FINISHED,
                thread_id=thread_id,
                run_id=run_id,
                outcome=E.RunFinishedInterruptOutcome(
                    type="interrupt", interrupts=[_interrupt_from_menu(menu)]
                ),
            )
        ]

    if event == "chat.stream_error":
        return [
            E.RunErrorEvent(
                type=E.EventType.RUN_ERROR,
                message=str(data.get("detail") or "stream failed"),
                code="stream_error",
            )
        ]

    if event == "session.error":
        return [
            E.RunErrorEvent(
                type=E.EventType.RUN_ERROR,
                message=str(data.get("message") or "session error"),
                code=str(data.get("code") or "session_error"),
            )
        ]

    if event == "chat.stream_cancelled":
        # NOT a RUN_ERROR: a human stopping the agent is a normal outcome, and
        # reporting it as an error puts a red state on a deliberate act.
        if run_id:
            return [
                E.RunFinishedEvent(
                    type=E.EventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id,
                    result={"cancelled": True,
                            "partial_len": data.get("partial_len") or 0},
                )
            ]
        return [_custom("stream.cancelled", data)]

    if event == "page.invalidate":
        # AG-UI has no native "a resource you are showing changed" event — its
        # state channel is intra-run, and this fires from outside any run (the
        # fleet, a schedule, another tab). MCP's `notifications/resources/updated`
        # is the right vocabulary and canopy is not an MCP server to a browser,
        # so it rides CUSTOM carrying the same payload: the URI, and nothing
        # else. The day FastMCP grows a server-side subscription API, this is the
        # line that changes.
        return [_custom("page.invalidate", {"uri": data.get("uri") or ""})]

    if event == "session.title_updated":
        return [
            E.StateDeltaEvent(
                type=E.EventType.STATE_DELTA,
                delta=[{"op": "replace", "path": "/title", "value": data.get("title") or ""}],
            )
        ]

    # Multiplayer and placement: canopy's, not AG-UI's. One user and one agent
    # is the protocol's model, so a co-edited draft, a presence roster and which
    # box a session is bound to have no native spelling. They are not dropped —
    # a canopy client needs them — they are namespaced.
    if event.startswith("draft.") or event.startswith("presence."):
        return [_custom(event, data)]

    # Unknown: silence. A projection that raises on an unrecognised frame would
    # let any new canopy event take down a third-party client's stream.
    return []


def project_state(*, page_state: dict | None, title: str = "") -> E.StateSnapshotEvent:
    """The shared state a client sees on connect.

    `page_state` is what the user is looking at (see `page_state.py`) and it is
    the same object `RunAgentInput.state` carries in the other direction — so
    the round trip is one vocabulary rather than two, which is the point of
    adopting a protocol at all.
    """
    snapshot: dict[str, Any] = {"title": title}
    if page_state:
        snapshot["page"] = page_state
    return E.StateSnapshotEvent(type=E.EventType.STATE_SNAPSHOT, snapshot=snapshot)


def encode(event: E.BaseEvent) -> dict:
    """Wire form: camelCase, and nothing absent spelled as null.

    `by_alias` is not cosmetic. The TypeScript and Go SDKs read `toolCallId`;
    emitting `tool_call_id` produces a stream that every non-Python client
    silently ignores — the exact failure this projection exists to avoid.
    """
    return event.model_dump(by_alias=True, exclude_none=True, mode="json")
