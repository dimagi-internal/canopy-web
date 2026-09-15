"""The AG-UI projection, through the socket that actually serves it.

`tests/test_agui_projection.py` proves the mapping. This file proves it is
REACHABLE — the distinction that `apps/mcp/page_tools.py` made expensive once,
when ten passing tests described a feature that no code path imported. A pure
function nobody calls is a design document with a test suite.

It also holds the fitness test. A projection is exactly the kind of thing that
rots silently: canopy adds a frame, nobody adds a mapping, and an AG-UI client
simply never hears about it — no error, no failing test, just a client that is
quietly less informed than a canopy one. So the frames the consumer can emit are
enumerated from the SOURCE and each must be either mapped or explicitly listed
as a decision somebody made.
"""

import re
from pathlib import Path

import pytest
from ag_ui.core import events as E
from pydantic import TypeAdapter

from apps.canopy_sessions import agui, consumers, stream_map

EVENT = TypeAdapter(E.Event)

#: BOTH producers. The consumer emits presence, drafts, menus and errors; the
#: STREAM frames — a streamed reply and every tool call — are built in
#: `stream_map.py` and merely passed through the consumer. Reading only
#: `consumers.py` found 13 frames and silently missed `chat.tool_use`,
#: `chat.stream_start` and `session.activity`: the most important ones, and
#: exactly what a fitness test is supposed to stop anyone from forgetting.
FRAME_SOURCES = [Path(m.__file__).read_text() for m in (consumers, stream_map)]

#: Frames the consumer emits that deliberately have no AG-UI spelling.
#:
#: Being listed here is a decision, not an oversight — which is the entire point
#: of the fitness test below. Each needs a reason:
#:
#:   session.state      — the connect snapshot. It is not one event but a whole
#:                        conversation plus participants plus drafts; an AG-UI
#:                        client gets MESSAGES_SNAPSHOT + STATE_SNAPSHOT built
#:                        from the same source, which is a different shape, not
#:                        a translation of this frame.
#:   session.stop       — a request to stop, travelling the other way. AG-UI
#:                        models cancellation on the client side, not as a
#:                        server event.
#:   session.page_action— canopy asking the PAGE to run something. It is the
#:                        host↔frame contract (page_actions.py), not the
#:                        agent↔UI one, and an AG-UI client is not the party
#:                        that executes it.
KNOWN_UNMAPPED = {
    "session.state",
    "session.stop",
    "session.page_action",
}


def _frames_the_consumer_can_emit() -> set[str]:
    """Every `"event": "..."` literal in either producer, read from the source.

    Reading the source rather than maintaining a list is what makes this a
    fitness test: a frame added tomorrow appears here without anyone choosing
    to add it, which is precisely the case that would otherwise slip.
    """
    found: set[str] = set()
    for source in FRAME_SOURCES:
        found |= set(re.findall(r'"event":\s*"([a-z_.]+)"', source))
    return found


def test_the_projection_covers_every_frame_the_consumer_can_emit():
    """The fitness test.

    A frame with no mapping and no entry in KNOWN_UNMAPPED means an AG-UI client
    silently never hears about something a canopy client does. The failure is
    invisible in production — no error, no dropped connection — so it has to be
    caught here or not at all.
    """
    emitted = _frames_the_consumer_can_emit()
    # Guard against the regex silently matching nothing, which would make this
    # test pass forever while checking nothing at all.
    # The floor is above the 13 that `consumers.py` alone yields, so dropping a
    # producer from FRAME_SOURCES fails here instead of quietly narrowing what
    # this test checks.
    assert len(emitted) >= 18, f"only found {sorted(emitted)}; a producer may be missing"
    # A frame only `stream_map` produces, named so that losing that producer is
    # a failure rather than a smaller passing test. Deliberately NOT
    # `chat.delta`: that is declared in the client protocol and mentioned in a
    # stream_map docstring as something a future runner would send, but nothing
    # on the server emits it today — asserting on it would pin a frame that does
    # not exist.
    assert "chat.tool_use" in emitted, "the streaming producer is not being read"

    unmapped = set()
    for name in emitted - KNOWN_UNMAPPED:
        if not agui.project({"event": name, "data": {}}, thread_id="t1", run_id="r1"):
            unmapped.add(name)

    assert not unmapped, (
        f"{sorted(unmapped)} reach a canopy client but not an AG-UI one. Add a "
        f"mapping in apps/canopy_sessions/agui.py, or add it to KNOWN_UNMAPPED "
        f"with the reason it has no AG-UI spelling."
    )


def test_known_unmapped_does_not_name_frames_that_no_longer_exist():
    """An entry left behind after a frame is deleted makes the exception list
    look considered while covering nothing."""
    stale = KNOWN_UNMAPPED - _frames_the_consumer_can_emit()

    assert not stale, f"{sorted(stale)} are listed as unmapped but nothing emits them"


# --- the seam itself ---------------------------------------------------------


class _Captured(consumers.SessionConsumer):
    """The consumer with its transport replaced, and nothing else.

    Driving the real class matters here: the projection lives in an override of
    `send_json`, so a test that called `agui.project` directly would pass with
    the override deleted — which is the exact shape of the bug this file exists
    to prevent.
    """

    def __init__(self):  # noqa: D107 - not a real consumer lifecycle
        self.sent = []
        self.session = None

    async def send_json(self, content, close=False):
        await consumers.SessionConsumer.send_json(self, content, close=close)

    async def send(self, *args, **kwargs):
        raise AssertionError("should not reach the socket")


class _Sink(_Captured):
    """Captures what the BASE class would have put on the wire."""

    async def _super_send_json(self, content, close=False):
        self.sent.append(content)


@pytest.fixture
def consumer(monkeypatch):
    c = _Sink()

    async def fake_send_json(self, content, close=False):
        c.sent.append(content)

    # Patch the Channels base so the override under test still runs.
    monkeypatch.setattr(
        consumers.AsyncJsonWebsocketConsumer, "send_json", fake_send_json, raising=True
    )
    return c


@pytest.mark.asyncio
async def test_a_client_that_does_not_ask_gets_canopys_own_frames_unchanged(consumer):
    """The property that makes this additive.

    `canopy-ui` is published to public npm at 0.7.0 and ace-web installs it, so
    a client that never heard of AG-UI must not be able to tell this shipped.
    """
    consumer.agui_mode = False

    await consumer.send_json({"event": "chat.delta", "data": {"message_id": "m1", "text": "hi"}})

    assert consumer.sent == [{"event": "chat.delta", "data": {"message_id": "m1", "text": "hi"}}]


@pytest.mark.asyncio
async def test_a_client_that_asks_gets_ag_ui_events(consumer):
    consumer.agui_mode = True

    await consumer.send_json({"event": "chat.delta", "data": {"message_id": "m1", "text": "hi"}})

    [frame] = consumer.sent
    assert frame["type"] == "TEXT_MESSAGE_CONTENT"
    assert frame["messageId"] == "m1"
    assert frame["delta"] == "hi"
    # And it parses with the SDK a real client would use.
    assert EVENT.validate_python(frame)


@pytest.mark.asyncio
async def test_one_canopy_frame_can_become_several_ag_ui_events(consumer):
    """A tool call is START + ARGS + END. A projection that could only emit one
    event per frame would have to drop two thirds of a tool call."""
    consumer.agui_mode = True

    await consumer.send_json(
        {"event": "chat.tool_use",
         "data": {"tool_message_id": "t9", "block": {"name": "list_insights", "input": {}}}}
    )

    assert [f["type"] for f in consumer.sent] == [
        "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
    ]


@pytest.mark.asyncio
async def test_a_frame_with_no_ag_ui_meaning_sends_nothing_rather_than_leaking(consumer):
    """The alternative — passing the canopy frame through untranslated — would
    put canopy's vocabulary on a stream a third-party client is parsing as
    AG-UI, where it is not merely useless but unparseable."""
    consumer.agui_mode = True

    await consumer.send_json({"event": "session.state", "data": {"messages": []}})

    assert consumer.sent == []


# --- negotiation -------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        (b"protocol=ag-ui", True),
        (b"token=x&protocol=ag-ui", True),
        (b"", False),
        (b"protocol=canopy", False),
        (None, False),
    ],
)
def test_the_protocol_is_negotiated_from_the_query_string(query, expected):
    """A browser cannot set headers on a WebSocket handshake, so the query
    string is the only place a plain client can ask."""
    c = _Sink()
    c.scope = {"query_string": query} if query is not None else {}

    c._negotiate_protocol()

    assert c.agui_mode is expected


def test_the_default_is_canopys_own_protocol():
    """Stated as its own test because it is the safety property: every existing
    client, including ones in another repo, keeps working by doing nothing."""
    assert consumers.SessionConsumer.agui_mode is False
