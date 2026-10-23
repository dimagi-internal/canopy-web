"""A turn's session is created once, whatever canopy does after the create.

Slack turn f7cb8694 (2026-10-23): emdash made the session, record-session 500'd
mid-deploy, the crash failed the turn with no session key, the server requeued it,
and the re-claim made a second session with the same prompt — while the web listed
the first as a session of its own beside the chat it belonged to."""
import types

import pytest

from canopy_runner import chat_bridge, execute, main as main_mod
from canopy_runner.client import ClientError


class _Client:
    def __init__(self, record_fails=0, transient=True):
        self.record_fails = record_fails
        self.transient = transient
        self.recorded = []
        self.finished = []
        self.failed = []
        self.events = []

    def resolve_session(self, *a, **k):
        return {"reuse": False}

    def start(self, *a, **k):
        pass

    def record_session(self, runner_id, agent, thread_key, **kw):
        if self.record_fails:
            self.record_fails -= 1
            raise ClientError("record-session -> 500", transient=self.transient,
                              status=500 if self.transient else 404)
        self.recorded.append((thread_key, kw.get("emdash_task_id")))

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        self.finished.append((turn_id, status, emdash_task_id))

    def fail_turn(self, turn_id, note):
        self.failed.append((turn_id, note))


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    chat_bridge.IN_FLIGHT.clear()
    creates = []

    def _create(*a, **k):
        creates.append(k.get("task_name"))
        return {"task": "c-hello-3bda"}

    monkeypatch.setattr(execute.cdp_control, "create_task", _create)
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("")
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: transcript)
    yield creates
    chat_bridge.IN_FLIGHT.clear()


def _cfg():
    return types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")


def _turn():
    return {"id": "t1", "agent_slug": "eva", "project": "", "workspace_slug": "dimagi",
            "prompt": "hello", "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"}}


def test_a_failed_record_does_not_end_the_chat_turn(_clean):
    client = _Client(record_fails=1)
    res = execute.execute_chat_turn(_cfg(), client, "r1", _turn())
    assert res == "chat:t1:c-hello-3bda", "the prompt is in the session; the reply is bridged"
    assert client.failed == [] and client.finished == []
    assert "c-hello-3bda" in execute._UNRECORDED
    assert {"status": "record_deferred", "task": "c-hello-3bda"} in [e["payload"] for e in client.events]


def test_the_deferred_record_lands_on_a_later_tick(_clean):
    client = _Client(record_fails=1)
    execute.execute_chat_turn(_cfg(), client, "r1", _turn())
    execute.retry_unrecorded(None, client)
    assert client.recorded == [("s1", "c-hello-3bda")]
    assert execute._UNRECORDED == {}


def test_a_refused_deferred_record_is_dropped(_clean):
    client = _Client(record_fails=2, transient=False)
    execute.execute_chat_turn(_cfg(), client, "r1", _turn())
    execute.retry_unrecorded(None, client)
    assert execute._UNRECORDED == {} and client.recorded == []


def test_a_reclaimed_turn_is_not_created_twice(_clean):
    client = _Client()
    execute.execute_chat_turn(_cfg(), client, "r1", _turn())
    chat_bridge.IN_FLIGHT.clear()
    res = execute.execute_chat_turn(_cfg(), client, "r1", _turn())
    assert res == "failed:t1"
    assert len(_clean) == 1, "one session per turn"
    assert client.finished == [("t1", "failed", "c-hello-3bda")], \
        "terminal, naming the session — a sessionless failure would be requeued"


def test_a_crash_after_the_create_names_the_session(_clean, monkeypatch, tmp_path):
    def _crash(cfg, client, runner_id, turn, cancel_check=None):
        execute._note_created(turn["id"], "c-hello-3bda")
        raise RuntimeError("boom")

    monkeypatch.setattr(execute, "execute_turn", _crash)
    client = _Client()
    client.claim = lambda *a, **k: _turn()
    cfg = types.SimpleNamespace(runner_id="r1", state_path=str(tmp_path / "state.json"))
    assert main_mod._claim_and_execute(cfg, client, paused=set()) == "failed:t1"
    assert client.failed == []
    assert client.finished == [("t1", "failed", "c-hello-3bda")]
