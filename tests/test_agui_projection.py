"""canopy's session frames, projected into AG-UI.

This file is the proof that was skipped once already. An earlier attempt at
"adopting AG-UI" shipped a page-state channel in canopy's own vocabulary and
described it as an AG-UI slice; a grep for the protocol's own symbols found four
matches, all of them in comments. So the tests here assert against the REAL
`ag_ui.core` models and the REAL wire encoding, never against a shape this repo
invented — because a mapping nobody validated against the actual SDK is a
mapping that only exists in a design doc.

Two properties matter more than the individual cases:

  * **the wire is camelCase.** The TypeScript and Go SDKs read `toolCallId`. A
    stream emitting `tool_call_id` is one every non-Python client silently
    ignores, which is precisely the failure adopting a protocol was meant to
    prevent — and it fails silently, so only a test catches it.
  * **an unknown frame is silence, never an exception.** This projection rides
    the same socket the product does; a new canopy event must not be able to
    take down a third-party client.
"""

import json
from pathlib import Path

import pytest
from ag_ui.core import events as E
from pydantic import TypeAdapter

from apps.canopy_sessions import agui

#: The SDK's own discriminated union over every event type. Parsing through it
#: is what makes the round-trip test real: it is the same machinery a Python
#: AG-UI client uses, so anything that fails here fails there.
EVENT = TypeAdapter(E.Event)

# --- the wire contract -------------------------------------------------------


def test_the_wire_is_camel_case_because_every_other_sdk_reads_it_that_way():
    """The single highest-consequence detail in the module.

    Python spells the field `tool_call_id`; the protocol spells it `toolCallId`.
    Emitting the Python spelling produces a stream that parses nowhere else and
    fails silently — a client sees an event of a known type with none of the
    fields it needs, and skips it.
    """
    [event] = agui.project(
        {"event": "chat.tool_result",
         "data": {"tool_message_id": "m9", "block": {"content": "done"}}},
        thread_id="t1",
    )

    wire = agui.encode(event)
    assert "toolCallId" in wire
    assert "tool_call_id" not in wire


def test_absent_fields_are_omitted_rather_than_sent_as_null():
    """AG-UI collapses absent and null in several places and the .NET SDK cannot
    tell them apart, so omission is the one spelling every SDK agrees on."""
    [event] = agui.project(
        {"event": "chat.stream_start", "data": {"message_id": "m1"}}, thread_id="t1"
    )

    wire = agui.encode(event)
    assert "name" not in wire
    assert None not in wire.values()


def test_every_projected_event_survives_a_round_trip_through_the_sdk():
    """The strongest single assertion here: whatever we emit, the protocol's own
    parser accepts. A hand-built dict that merely looks right is how a mapping
    drifts from the spec it claims to implement."""
    frames = [
        {"event": "chat.stream_start", "data": {"message_id": "m1"}},
        {"event": "chat.delta", "data": {"message_id": "m1", "text": "hi"}},
        {"event": "chat.stream_complete", "data": {"message_id": "m1", "plaintext": "hi"}},
        {"event": "chat.user_message", "data": {"message_id": "u1", "plaintext": "hello"}},
        {"event": "chat.tool_use",
         "data": {"tool_message_id": "t9", "parent_message_id": "m1",
                  "block": {"type": "tool_use", "id": "toolu_01ABC",
                        "name": "list_insights", "input": {"limit": 5}}}},
        {"event": "chat.tool_result",
         "data": {"tool_message_id": "t9", "block": {"content": "[]"}}},
        {"event": "session.activity", "data": {"state": "working"}},
        {"event": "session.error", "data": {"code": "nope", "message": "bad"}},
        {"event": "chat.stream_error", "data": {"message_id": "m1", "detail": "boom"}},
        {"event": "session.title_updated", "data": {"title": "New"}},
        {"event": "draft.updated", "data": {"id": "d1", "body": "x"}},
    ]

    for frame in frames:
        for event in agui.project(frame, thread_id="t1", run_id="r1"):
            wire = agui.encode(event)
            # Parsing with the SDK's own discriminated union is the real check.
            assert EVENT.validate_python(wire) is not None, frame


# --- streaming ---------------------------------------------------------------


def test_a_streamed_reply_becomes_start_content_end():
    start = agui.project({"event": "chat.stream_start", "data": {"message_id": "m1"}},
                         thread_id="t1")
    delta = agui.project({"event": "chat.delta", "data": {"message_id": "m1", "text": "he"}},
                         thread_id="t1")
    end = agui.project({"event": "chat.stream_complete", "data": {"message_id": "m1"}},
                       thread_id="t1")

    assert [e.type for e in start + delta + end] == [
        E.EventType.TEXT_MESSAGE_START,
        E.EventType.TEXT_MESSAGE_CONTENT,
        E.EventType.TEXT_MESSAGE_END,
    ]
    assert start[0].role == "assistant"
    assert delta[0].delta == "he"


def test_an_empty_delta_is_dropped_rather_than_emitted():
    """AG-UI rejects an empty delta outright, and canopy emits one on a
    keepalive. Dropping is right: an empty chunk changes nothing, and raising
    would take down the stream over a no-op."""
    assert agui.project({"event": "chat.delta", "data": {"message_id": "m1", "text": ""}},
                        thread_id="t1") == []


def test_a_message_typed_into_emdash_arrives_as_a_user_chunk():
    """It arrives whole rather than streamed, which is what CHUNK is for."""
    [event] = agui.project(
        {"event": "chat.user_message", "data": {"message_id": "u1", "plaintext": "hello"}},
        thread_id="t1",
    )

    assert event.type == E.EventType.TEXT_MESSAGE_CHUNK
    assert event.role == "user"
    assert event.delta == "hello"


# --- tool calls --------------------------------------------------------------


def test_a_tool_call_becomes_start_args_end_with_one_correlating_id():
    """Two id spaces for one call is how a tool update merges into nothing, so
    canopy's `tool_message_id` is reused rather than a second id minted."""
    events = agui.project(
        {"event": "chat.tool_use",
         "data": {"tool_message_id": "t9", "parent_message_id": "m1",
                  "block": {"name": "clear_insights", "input": {"category": "stale"}}}},
        thread_id="t1",
    )

    assert [e.type for e in events] == [
        E.EventType.TOOL_CALL_START,
        E.EventType.TOOL_CALL_ARGS,
        E.EventType.TOOL_CALL_END,
    ]
    assert {e.tool_call_id for e in events} == {"t9"}
    assert events[0].tool_call_name == "clear_insights"


def test_tool_arguments_are_a_json_string_not_an_object():
    """AG-UI streams arguments as a string precisely because fragments need not
    be individually valid JSON. Sending an object would parse here and nowhere
    that follows the spec."""
    events = agui.project(
        {"event": "chat.tool_use",
         "data": {"tool_message_id": "t9", "block": {"name": "x", "input": {"limit": 5}}}},
        thread_id="t1",
    )

    args = events[1].delta
    assert isinstance(args, str)
    assert json.loads(args) == {"limit": 5}


def test_a_tool_call_with_no_input_still_sends_args():
    """An omitted ARGS frame leaves a client with a call it cannot complete."""
    events = agui.project(
        {"event": "chat.tool_use", "data": {"tool_message_id": "t9", "block": {"name": "x"}}},
        thread_id="t1",
    )

    assert json.loads(events[1].delta) == {}


def test_a_tool_result_correlates_to_the_call_that_produced_it():
    [event] = agui.project(
        {"event": "chat.tool_result",
         "data": {"tool_message_id": "t9", "block": {"content": "ok"}}},
        thread_id="t1",
    )

    assert event.tool_call_id == "t9"
    assert event.content == "ok"


def test_a_structured_tool_result_is_serialised_not_dropped():
    [event] = agui.project(
        {"event": "chat.tool_result",
         "data": {"tool_message_id": "t9", "block": {"content": [{"id": 1}]}}},
        thread_id="t1",
    )

    assert json.loads(event.content) == [{"id": 1}]


# --- the blocked agent, which is the interesting one -------------------------

MENU = {
    "question": "Proceed to Phase 4?",
    "title": "Phase gate",
    "body": "Phase 4 is test-gated.",
    "source": "transcript",
    "observed_at": 1789000000,
    "options": [{"number": 1, "label": "Proceed"}, {"number": 2, "label": "Stop"}],
    "questions": [
        {"index": 0, "question": "Proceed to Phase 4?", "multi_select": False,
         "options": [{"number": 1, "label": "Proceed", "description": "test-gated"},
                     {"number": 2, "label": "Stop"}]},
        {"index": 1, "question": "Notify whom?", "multi_select": True,
         "options": [{"number": 1, "label": "me"}, {"number": 2, "label": "team"}]},
    ],
}


def test_a_blocked_agent_pauses_the_run_rather_than_failing_it():
    """"A blocked turn is not a wedged one" — canopy's rule, and AG-UI's shape
    for it is a RUN_FINISHED carrying an interrupt outcome. Reporting it as an
    error would put a red state on an agent that is politely waiting."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}},
                           thread_id="t1", run_id="r1")

    assert event.type == E.EventType.RUN_FINISHED
    assert event.outcome.type == "interrupt"
    assert len(event.outcome.interrupts) == 1


def test_the_interrupt_carries_its_own_staleness():
    """`expires_at` is the correction the "clicking does nothing" incident paid
    for: the dialog lives on a terminal and this object is a copy, so without an
    age the only way to learn the copy is stale is to tap it and be refused."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}},
                           thread_id="t1", run_id="r1")

    assert event.outcome.interrupts[0].expires_at.startswith("2026-")


def test_a_menu_with_no_observed_at_admits_it_does_not_know():
    """Rather than defaulting to a far future, which would make the client stop
    warning about exactly the case that matters."""
    [event] = agui.project(
        {"event": "session.menu", "data": {"menu": {**MENU, "observed_at": None}}},
        thread_id="t1", run_id="r1",
    )

    assert event.outcome.interrupts[0].expires_at is None


def test_every_question_reaches_the_response_schema_not_only_the_first():
    """The TUI draws questions as TABS and will not submit until each has an
    answer, so a surface rendering only the first cannot complete the ask no
    matter which button is pressed (eva, 2026-08-12)."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}},
                           thread_id="t1", run_id="r1")

    props = event.outcome.interrupts[0].response_schema["properties"]
    assert set(props) == {"q0", "q1"}


def test_a_multi_select_question_is_typed_as_a_list():
    """A number key TOGGLES a checkbox rather than answering it, so a client
    that cannot see multi_select renders the wrong control AND presses the
    wrong key."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}},
                           thread_id="t1", run_id="r1")

    props = event.outcome.interrupts[0].response_schema["properties"]
    assert props["q0"]["type"] == "string"
    assert props["q1"]["type"] == "array"


def test_a_single_question_menu_still_produces_a_schema():
    """A producer older than multi-question support sends only the bare fields,
    which still describe question 1."""
    bare = {"question": "Continue?", "options": [{"number": 1, "label": "Yes"}]}

    [event] = agui.project({"event": "session.menu", "data": {"menu": bare}},
                           thread_id="t1", run_id="r1")

    assert event.outcome.interrupts[0].response_schema["properties"]["q0"]["enum"] == ["Yes"]


def test_the_body_survives_because_it_is_what_makes_a_dialog_answerable():
    """"Do you want to proceed?" tells you nothing without the command it
    means — and AG-UI has no field for it, so it rides metadata."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}},
                           thread_id="t1", run_id="r1")

    assert event.outcome.interrupts[0].metadata["canopy"]["body"] == "Phase 4 is test-gated."


def test_a_retracted_menu_is_announced_even_though_the_protocol_has_no_word_for_it():
    """Somebody answered at the keyboard. AG-UI has no "interrupt withdrawn",
    so it rides CUSTOM; a client that ignores it keeps showing a dialog that
    will refuse, which is the pre-existing behaviour, not a regression."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": None}},
                           thread_id="t1", run_id="r1")

    assert event.type == E.EventType.CUSTOM
    assert event.name == "canopy.menu.retracted"


def test_a_menu_with_no_run_does_not_fabricate_one():
    """An interrupt outcome belongs to a RUN. A made-up run id is one no resume
    could ever match, which converts a visible gap into a silent one."""
    [event] = agui.project({"event": "session.menu", "data": {"menu": MENU}}, thread_id="t1")

    assert event.type == E.EventType.CUSTOM
    assert event.name == "canopy.menu"


# --- stopping, erroring, and the difference ---------------------------------


def test_a_human_stopping_the_agent_is_not_an_error():
    """Reporting a deliberate act as an error puts a red state on the user's own
    decision."""
    [event] = agui.project(
        {"event": "chat.stream_cancelled", "data": {"message_id": "m1", "partial_len": 12}},
        thread_id="t1", run_id="r1",
    )

    assert event.type == E.EventType.RUN_FINISHED
    assert event.result["cancelled"] is True


def test_a_stream_failure_is_an_error():
    [event] = agui.project(
        {"event": "chat.stream_error", "data": {"message_id": "m1", "detail": "boom"}},
        thread_id="t1",
    )

    assert event.type == E.EventType.RUN_ERROR
    assert event.message == "boom"


# --- what canopy has and AG-UI does not -------------------------------------


def test_multiplayer_is_namespaced_rather_than_dropped():
    """AG-UI models ONE user and one agent. canopy's chat is co-edited and has a
    presence roster, so these have no native spelling — but a canopy client
    needs them, and dropping them would make the projection lossy."""
    [draft] = agui.project({"event": "draft.updated", "data": {"id": "d1", "body": "x"}},
                           thread_id="t1")
    [presence] = agui.project({"event": "presence.joined", "data": {"user_id": 3}},
                              thread_id="t1")

    assert draft.name == "canopy.draft.updated"
    assert presence.name == "canopy.presence.joined"
    # Namespaced so a reader can tell "the protocol says this" from "canopy
    # says this", and so a future AG-UI event with the same idea cannot collide.
    assert draft.name.startswith(agui.CUSTOM_PREFIX)


def test_the_agents_own_state_rides_the_designed_activity_extension_point():
    """`activity_type` is an open string on purpose. Using it is using AG-UI as
    intended, not working around it."""
    events = agui.project({"event": "session.activity", "data": {"state": "blocked"}},
                          thread_id="t1")

    snapshot = next(e for e in events if e.type == E.EventType.ACTIVITY_SNAPSHOT)
    assert snapshot.activity_type == agui.ACTIVITY_TYPE
    assert snapshot.content == {"state": "blocked"}


# --- the projection must never break the socket it rides on ------------------


def test_an_unknown_frame_is_silence_not_an_exception():
    """A new canopy event must not be able to take down a third-party client."""
    assert agui.project({"event": "session.something_new", "data": {"x": 1}},
                        thread_id="t1") == []


@pytest.mark.parametrize(
    "frame",
    [{}, {"event": None}, {"event": "chat.delta"}, {"event": "chat.tool_use", "data": {}}],
)
def test_a_malformed_frame_does_not_raise(frame):
    agui.project(frame, thread_id="t1")


# --- the cross-language fixture ---------------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "frontend" / "packages" / \
    "canopy-ui" / "src" / "chat" / "agui.fixture.json"

#: Canopy frames whose projection both languages agree on.
#:
#: The projection lives in Python and its INVERSE lives in TypeScript, so
#: nothing in either codebase can tell you the two still agree. A committed
#: fixture is the only artifact both can read: Python asserts it is current,
#: TypeScript asserts its inverse recovers the original frame. Drift in either
#: direction fails a test instead of silently producing a client that renders a
#: subtly different conversation.
ROUND_TRIP_FRAMES = [
    {"event": "chat.stream_start", "data": {"message_id": "m1", "turn_index": 4}},
    {"event": "chat.delta", "data": {"message_id": "m1", "text": "hello"}},
    {"event": "chat.stream_complete", "data": {"message_id": "m1", "plaintext": "hello"}},
    {"event": "chat.user_message",
     "data": {"message_id": "u1", "turn_index": 3, "plaintext": "hi there"}},
    {"event": "chat.tool_use",
     "data": {"tool_message_id": "t9", "parent_message_id": "m1", "turn_index": 5,
              "block": {"name": "list_insights", "input": {"limit": 5}}}},
    {"event": "chat.tool_result",
     "data": {"tool_message_id": "t9", "parent_message_id": "m1", "turn_index": 6,
              "block": {"type": "tool_result", "tool_use_id": "toolu_01ABC",
                        "content": "[]"}}},
    {"event": "session.title_updated", "data": {"title": "Insights triage"}},
    {"event": "draft.updated", "data": {"id": "d1", "body": "x", "version": 2}},
    {"event": "presence.joined", "data": {"user_id": 7}},
    {"event": "presence.left", "data": {"user_id": 7}},
    # --- every other frame a canopy client can receive ------------------------
    # Added 2026-09-18, when `test_every_frame_the_consumer_emits_is_in_the_
    # round_trip_fixture` arrived and found twelve frames that had never been
    # proven to survive the trip. Shapes copied from the producers
    # (`consumers.py`, `stream_map.py`, `page_actions.py`), not invented, so a
    # producer changing its payload shows up here as a stale example.
    {"event": "session.state",
     "data": {"messages": [
                  {"id": "41", "turn_index": 0, "role": "user", "content": {},
                   "plaintext": "what is stale?", "status": "complete",
                   "error_detail": None, "started_at": None, "completed_at": None,
                   "created_at": "2026-09-18T12:00:00+00:00"},
                  {"id": "42", "turn_index": 1, "role": "assistant", "content": {},
                   "plaintext": "Three insights are.", "status": "complete",
                   "error_detail": None, "started_at": None, "completed_at": None,
                   "created_at": "2026-09-18T12:00:05+00:00"}],
              "active_draft": None,
              "participants": [{"user_id": 7, "role": "editor"}],
              "presence_user_ids": [7],
              "current_user_id": 7,
              "menu": None}},
    {"event": "session.page_action",
     "data": {"id": "9f1c", "name": "scrollToRow", "args": {"id": 4471}}},
    {"event": "session.stop", "data": {"state": "failed"}},
    {"event": "session.activity", "data": {"state": "working"}},
    {"event": "session.menu",
     "data": {"menu": {"source": "hook", "question": "Proceed?", "observed_at": 1758196800,
                       "options": [{"label": "Yes"}, {"label": "No"}]}}},
    {"event": "session.error", "data": {"code": "draft_conflict", "message": "stale version"}},
    {"event": "chat.stream_cancelled", "data": {"message_id": "m1", "partial_len": 12}},
    {"event": "chat.stream_error", "data": {"message_id": "m1", "detail": "runner went away"}},
    {"event": "draft.committed", "data": {"draft_id": "d1", "user_message_id": "u2"}},
    {"event": "draft.discarded", "data": {"draft_id": "d1"}},
    {"event": "draft.lock_changed",
     "data": {"draft_id": "d1", "holder_user_id": 7, "expires_at": "2026-09-18T12:01:00+00:00"}},
    {"event": "page.invalidate", "data": {"uri": "item://"}},
]


def _build_fixture() -> list[dict]:
    return [
        {"canopy": frame,
         # NO run id, because the live socket passes none
         # (`SessionConsumer.send_json`). This passed `run_id="r1"` until
         # 2026-09-18, so the round trip proved a path production never takes —
         # and missed that the path it does take renamed a cancelled stream to
         # an event the reducer does not know.
         "agui": [agui.encode(e) for e in agui.project(frame, thread_id="t1")]}
        for frame in ROUND_TRIP_FRAMES
    ]


def test_the_shared_fixture_is_current():
    """Regenerate with `python -m tests.regen_agui_fixture` when the projection
    changes on purpose; a failure here means Python and TypeScript have drifted
    and the TS round-trip test is checking a stale contract."""
    assert FIXTURE.exists(), f"{FIXTURE} is missing; regenerate it"
    assert json.loads(FIXTURE.read_text()) == _build_fixture()


def test_the_fixture_covers_the_frames_a_conversation_actually_uses():
    """A fixture holding only the easy cases would pass forever while proving
    nothing about a streamed reply or a tool call."""
    covered = {entry["canopy"]["event"] for entry in _build_fixture()}

    assert {"chat.stream_start", "chat.delta", "chat.stream_complete",
            "chat.tool_use", "chat.tool_result"} <= covered
