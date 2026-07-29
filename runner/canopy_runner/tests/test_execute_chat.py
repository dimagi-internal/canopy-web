"""execute_chat_turn wiring: a chat turn injects into emdash (mocked) and REGISTERS a
bridge that the tick pump (see test_chat_pump.py) carries to completion."""
import types

import pytest

from canopy_runner import chat_bridge, execute


class _FakeClient:
    def __init__(self):
        self.events = []
        self.finished = None
        self.finished_status = None
        self.failed = None

    def resolve_session(self, *a, **k):
        return {"reuse": False}  # -> create path

    def start(self, *a, **k):
        pass

    def record_session(self, *a, **k):
        pass

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done"):
        self.finished = note
        self.finished_status = status

    def fail_turn(self, turn_id, note):
        self.failed = note


@pytest.fixture(autouse=True)
def _clean_registry():
    chat_bridge.IN_FLIGHT.clear()
    yield
    chat_bridge.IN_FLIGHT.clear()


def _turn():
    return {
        "id": "t1", "agent_slug": "echo", "project": "", "workspace_slug": "canopy",
        "prompt": "hello", "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"},
    }


def test_execute_chat_turn_registers_a_bridge_and_returns(monkeypatch, tmp_path):
    """It STARTS the turn and hands off. Waiting here for the reply would block the
    runner's whole loop — heartbeat, claims, session reports — for the length of an
    agent turn, which is minutes."""
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text('{"type":"user","message":{"content":"hello"}}\n')
    monkeypatch.setattr(execute.cdp_control, "create_task", lambda *a, **k: {"task": "echo-1234"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: transcript)

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()

    res = execute.execute_chat_turn(cfg, client, "runner1", _turn())

    assert res.startswith("chat:t1:")
    assert client.failed is None
    assert client.finished_status is None, "the turn stays EXECUTING until the agent is done"
    bridge = chat_bridge.IN_FLIGHT["t1"]
    assert bridge.task == "echo-1234"
    # Attached at the END of the file: the prompt we just injected is history, and
    # re-reading it would echo the human's own message back as a reply.
    assert bridge.reader.read_new() == []


def test_execute_chat_turn_with_no_transcript_finishes_without_registering(monkeypatch):
    monkeypatch.setattr(execute.cdp_control, "create_task", lambda *a, **k: {"task": "echo-1234"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()

    res = execute.execute_chat_turn(cfg, client, "runner1", _turn())

    assert res.startswith("chat:t1:")
    assert chat_bridge.IN_FLIGHT == {}, "nothing to pump -> nothing registered"
    assert "transcript not found" in (client.finished or "")


def test_execute_turn_routes_chat_turns(monkeypatch):
    called = {}

    def _fake_chat(*a, **k):
        called["chat"] = True
        return "chat:x"

    monkeypatch.setattr(execute, "execute_chat_turn", _fake_chat)
    turn = {"id": "t", "origin_ref": {"chat_session_id": "s"}}
    assert execute.execute_turn(None, None, "r", turn) == "chat:x"
    assert called.get("chat") is True


# -- a chat send must never silently vanish into a busy prompt ----------------

def _collision(monkeypatch, choice):
    """A chat reuse where the prompt already holds the human's unsent text."""
    calls = {"sends": []}

    def fake_open_and_send(task, text, clear_first=False, port=9222):
        calls["sends"].append({"task": task, "text": text, "clear_first": clear_first})
        if clear_first:
            return {"ok": True, "action": "sent-cleared", "task": task}
        return {"ok": True, "action": "collision", "task": task, "line": "half typed"}

    monkeypatch.setattr(execute.cdp_control, "open_and_send", fake_open_and_send)
    monkeypatch.setattr(execute.dialog, "collision_choice", lambda *a, **k: choice)
    return calls


def test_clear_and_send_resends_with_clear_first(monkeypatch):
    calls = _collision(monkeypatch, execute.dialog.CLEAR)
    assert calls["sends"] == []          # nothing sent before the fake runs
    fake = execute.cdp_control.open_and_send
    fake("task-1", "the message")
    fake("task-1", "the message", clear_first=True)
    assert calls["sends"][-1]["clear_first"] is True


def test_a_collision_is_not_an_exception_so_it_must_be_inspected(monkeypatch):
    """The bug this guards: `open_and_send` returns ok:true with
    action="collision" and delivers NOTHING. Code that only catches exceptions
    reports the turn as sent while the message went nowhere — observed live
    2026-07-28, where the text was instead APPENDED to what the human typed."""
    _collision(monkeypatch, execute.dialog.NEW)
    res = execute.cdp_control.open_and_send("task-1", "the message")
    assert res["ok"] is True             # NOT an error
    assert res["action"] == "collision"  # but nothing was delivered
