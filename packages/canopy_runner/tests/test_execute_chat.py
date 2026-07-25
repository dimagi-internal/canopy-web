"""execute_chat_turn wiring: a chat turn injects into emdash (mocked) and bridges the
assistant reply that appears in the transcript AFTER injection back into the ledger."""
import types

from canopy_runner import execute


def _asst(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


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


def test_execute_chat_turn_bridges_the_reply(monkeypatch):
    # emdash transcript GROWS: only the prompt at bridge-start, then the assistant reply.
    states = [
        [_user("hello")],
        [_user("hello"), _asst("Hi there!")],
        [_user("hello"), _asst("Hi there!")],  # stable -> idle completion
    ]
    box = {"i": 0}

    def fake_read(_path):
        i = min(box["i"], len(states) - 1)
        box["i"] += 1
        return states[i]

    monkeypatch.setattr(execute.cdp_control, "create_task", lambda *a, **k: {"task": "echo-1234"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: "/tmp/fake.jsonl")
    monkeypatch.setattr(execute.chat_bridge, "read_records", fake_read)
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)  # instant polls

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()
    turn = {
        "id": "t1", "agent_slug": "echo", "project": "", "workspace_slug": "canopy",
        "prompt": "hello", "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"},
    }

    res = execute.execute_chat_turn(cfg, client, "runner1", turn)

    assert res.startswith("chat:t1:")
    assert client.failed is None
    # the assistant reply was bridged as an assistant TurnEvent
    assistant_events = [e for e in client.events if e.get("kind") == "assistant"]
    assert [e["payload"]["text"] for e in assistant_events] == ["Hi there!"]
    assert "bridged" in (client.finished or "")
    assert client.finished_status == "done"  # not cancelled -> the default finish status


def test_execute_turn_routes_chat_turns(monkeypatch):
    called = {}

    def _fake_chat(*a, **k):
        called["chat"] = True
        return "chat:x"

    monkeypatch.setattr(execute, "execute_chat_turn", _fake_chat)
    turn = {"id": "t", "origin_ref": {"chat_session_id": "s"}}
    assert execute.execute_turn(None, None, "r", turn) == "chat:x"
    assert called.get("chat") is True


def test_execute_turn_threads_cancel_check_to_chat_turn(monkeypatch):
    """execute_turn must pass its cancel_check kwarg straight through to
    execute_chat_turn — this is the seam main.py's CANCELLED_TURNS membership check
    rides on all the way down to the bridge poll."""
    captured = {}

    def _fake_chat(cfg, client, runner_id, turn, cancel_check=None):
        captured["cancel_check"] = cancel_check
        return "chat:x"

    monkeypatch.setattr(execute, "execute_chat_turn", _fake_chat)
    turn = {"id": "t", "origin_ref": {"chat_session_id": "s"}}
    sentinel = lambda tid: True  # noqa: E731
    assert execute.execute_turn(None, None, "r", turn, cancel_check=sentinel) == "chat:x"
    assert captured["cancel_check"] is sentinel


def test_execute_chat_turn_cancel_interrupts_and_finishes_cancelled(monkeypatch):
    """A cancelled chat turn must: stop the bridge immediately (should_stop breaks the
    poll loop rather than waiting out the idle window), press Escape in the emdash
    session via cdp_control.interrupt, and finish the turn CANCELLED (not done) with a
    human-readable note — never the normal 'chat reply bridged' note."""
    # The transcript never idles on its own — only cancellation should end the bridge.
    monkeypatch.setattr(execute.cdp_control, "create_task", lambda *a, **k: {"task": "echo-1234"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: "/tmp/fake.jsonl")
    monkeypatch.setattr(execute.chat_bridge, "read_records", lambda _p: [_user("hello")])
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)

    interrupted = {}
    monkeypatch.setattr(execute.cdp_control, "interrupt",
                        lambda task, port=None: interrupted.update(task=task, port=port))

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()
    turn = {
        "id": "t1", "agent_slug": "echo", "project": "", "workspace_slug": "canopy",
        "prompt": "hello", "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"},
    }

    res = execute.execute_chat_turn(cfg, client, "runner1", turn, cancel_check=lambda tid: True)

    assert res == "cancelled:t1"
    assert interrupted == {"task": "echo-1234", "port": 9222}
    assert client.finished_status == "cancelled"
    assert client.finished == "cancelled by user"


def test_execute_chat_turn_cancel_survives_a_failed_interrupt(monkeypatch):
    """The turn must still finish CANCELLED even if the CDP interrupt itself blows up
    (emdash closed, sidecar crashed, etc.) — cancel must not get stuck because the
    Escape press failed."""
    monkeypatch.setattr(execute.cdp_control, "create_task", lambda *a, **k: {"task": "echo-1234"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: "/tmp/fake.jsonl")
    monkeypatch.setattr(execute.chat_bridge, "read_records", lambda _p: [_user("hello")])
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)

    def _boom(*a, **k):
        raise execute.cdp_control.CDPError("emdash gone")

    monkeypatch.setattr(execute.cdp_control, "interrupt", _boom)

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()
    turn = {
        "id": "t1", "agent_slug": "echo", "project": "", "workspace_slug": "canopy",
        "prompt": "hello", "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"},
    }

    res = execute.execute_chat_turn(cfg, client, "runner1", turn, cancel_check=lambda tid: True)

    assert res == "cancelled:t1"
    assert client.finished_status == "cancelled"
