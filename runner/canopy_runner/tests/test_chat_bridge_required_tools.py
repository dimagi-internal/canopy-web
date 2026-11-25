"""A confined chat session must not answer a caller from nothing.

A caller's profile names the MCP tools its session works through (who_is_asking,
site_call, ...). If the session's canopy-web server never authenticated, Claude
Code offers only `authenticate` placeholders, and the agent answers the person
without any data ("I can't see your payment record from here"). The bridge holds
a confined turn's text until the session has at least one tool its profile names,
and fails the turn when it hands back without one, so a caller such as labs'
worker-chat bridge gives the thread to a person instead."""
import json
import types

import pytest

from canopy_runner import cancel, chat_bridge, chat_pump
from canopy_runner.chat_bridge import LiveBridge, required_mcp_tools, tools_in_transcript

PROFILE_TOOLS = ["Read", "Skill", "ToolSearch", "mcp__*canopy-web__who_is_asking",
                 "mcp__*canopy-web__site_call", "mcp__*canopy-web__page_*"]
REQUIRED = required_mcp_tools({"tools": PROFILE_TOOLS})
PLACEHOLDERS = ["mcp__plugin_canopy_canopy-web__authenticate",
                "mcp__plugin_canopy_canopy-web__complete_authentication", "mcp__easel__chart"]
REAL = ["mcp__plugin_canopy_canopy-web__who_is_asking", "mcp__plugin_canopy_canopy-web__site_call"]


def _delta(added=(), removed=(), pending=()):
    return {"type": "attachment", "attachment": {
        "type": "deferred_tools_delta", "addedNames": list(added), "removedNames": list(removed),
        "pendingMcpServers": list(pending)}}


def _asst(text, stop="tool_use"):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}],
                                            "stop_reason": stop}}


class _Reader:
    def __init__(self, batches):
        self.batches = list(batches)

    def read_new(self):
        return self.batches.pop(0) if self.batches else []


def _bridge(batches=(), required=REQUIRED, seen=()):
    return LiveBridge(turn_id="t1", task="cx-1", reader=_Reader(batches),
                      required_tools=required, tools_seen=set(seen))


def test_only_the_profiles_mcp_tools_are_required():
    assert REQUIRED == ("mcp__*canopy-web__who_is_asking", "mcp__*canopy-web__site_call",
                        "mcp__*canopy-web__page_*")
    assert required_mcp_tools({"tools": ["Read"]}) == ()
    assert required_mcp_tools(None) == ()


def test_an_unauthenticated_session_never_relays_its_answer_and_fails():
    b = _bridge()
    b.step([_delta(PLACEHOLDERS), _asst("I can't see your payment record from here.", "end_turn")])
    assert b.pending == []
    assert b.done_reason == "missing_tools" and b.finished
    assert "who_is_asking" in b.note and "authenticate" in b.note


def test_an_authenticated_session_streams_as_before():
    b = _bridge()
    b.step([_delta(PLACEHOLDERS[2:] + REAL), _asst("Your last payment was ...", "end_turn")])
    assert b.pending == ["Your last payment was ..."]
    assert b.done_reason == "end_turn"


def test_text_written_while_the_server_was_still_connecting_is_released_once_it_lands():
    b = _bridge()
    b.step([_delta(["mcp__easel__chart"], pending=["plugin:canopy:canopy-web"]),
            _asst("Let me look that up.")])
    assert b.pending == [] and not b.done_reason
    b.step([_delta(REAL), _asst("Here it is.", "end_turn")])
    assert b.pending == ["Let me look that up.", "Here it is."]
    assert b.done_reason == "end_turn"


def test_a_removed_tool_no_longer_counts():
    b = _bridge(seen=REAL)
    b.step([_delta(removed=REAL), _asst("answer", "end_turn")])
    assert b.done_reason == "missing_tools"


def test_a_reused_session_is_seeded_from_the_transcript_before_the_send(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [_delta(PLACEHOLDERS), _delta(REAL), _asst("earlier", "end_turn")]
    p.write_text("".join(json.dumps(r) + "\n" for r in lines) + "{not json\n")
    seen, deltas = tools_in_transcript(str(p), upto=p.stat().st_size)
    assert set(REAL) <= seen and deltas == 2
    b = _bridge(seen=seen)
    b.step([_asst("now", "end_turn")])
    assert b.pending == ["now"]


def test_a_session_that_never_reports_its_tools_is_not_gated():
    """No deferred_tools_delta at all (a Claude Code that lists every tool up front)
    tells us nothing either way; failing every confined turn on it would be worse."""
    b = _bridge()
    b.step([_asst("answer", "end_turn")])
    assert b.pending == ["answer"] and b.done_reason == "end_turn"


def test_a_full_profile_turn_is_never_gated():
    b = _bridge(required=())
    b.step([_delta(PLACEHOLDERS), _asst("answer", "end_turn")])
    assert b.pending == ["answer"] and b.done_reason == "end_turn"


class _FakeClient:
    def __init__(self):
        self.events, self.finished = [], []

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        self.finished.append((turn_id, status, note))


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_bridge.IN_FLIGHT.clear()
    cancel.CANCELLED_TURNS.clear()
    yield
    chat_bridge.IN_FLIGHT.clear()


def test_the_pump_fails_the_turn_and_says_why_without_relaying_the_answer(monkeypatch):
    monkeypatch.setattr(chat_pump, "flush_turn_transcript", lambda *a, **k: None)
    client = _FakeClient()
    chat_bridge.IN_FLIGHT["t1"] = _bridge([[_delta(PLACEHOLDERS),
                                            _asst("I can't see that from here.", "end_turn")]])
    chat_pump.pump_chat_bridges(types.SimpleNamespace(cdp_port=9222), client)
    assert not any(e["kind"] == "assistant" for e in client.events)
    assert [e["kind"] for e in client.events] == ["error"]
    assert client.finished and client.finished[0][1] == "failed"
    assert "who_is_asking" in client.finished[0][2]
    assert chat_bridge.IN_FLIGHT == {}
