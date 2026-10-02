"""A reuse send is CONFIRMED against the session transcript, or the turn fails.

The incident: turn ffaa56ce (2026-10-02) — `open_and_send` reported `sent`, the
text never reached Claude Code, and the turn sat RUNNING with no reply and no
signal. These tests pin that a send nothing in the transcript acknowledges now
fails the turn with a note the human can act on, and that a send that DID land —
taken, queued, or rewritten by the TUI — is never reported lost (a false loss makes
the human resend, which duplicates the message).
"""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import cdp_control, chat_bridge, delivery, emdash, execute


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(delivery, "CONFIRM_TIMEOUT", 0.3)
    monkeypatch.setattr(delivery, "CONFIRM_POLL", 0.02)
    monkeypatch.setattr(delivery, "UNMATCHED_GRACE", 0.1)
    monkeypatch.setattr(emdash, "task_state", lambda *a, **k: "live")
    chat_bridge.IN_FLIGHT.clear()
    yield
    chat_bridge.IN_FLIGHT.clear()


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}, "origin": {"kind": "human"}}


# ── classification ─────────────────────────────────────────────────────────

def test_a_taken_prompt_is_confirmed():
    assert delivery.classify([_user("hello   there\nworld")], "hello there\nworld") == delivery.CONFIRMED


def test_a_queued_prompt_is_confirmed():
    rec = {"type": "queue-operation", "operation": "enqueue", "content": "the long message"}
    assert delivery.classify([rec], "the long message") == delivery.CONFIRMED


def test_tool_results_and_dequeues_are_not_prompts():
    tool = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "the long message"}]}}
    deq = {"type": "queue-operation", "operation": "dequeue"}
    assert delivery.classify([tool, deq, {"type": "assistant"}], "the long message") is None


def test_a_rewritten_prompt_counts_as_delivered_not_lost():
    """Claude Code may collapse a paste — that must never read as a loss."""
    assert delivery.classify([_user("[Pasted text #1 +40 lines]")], "x" * 300) == delivery.UNMATCHED


def test_the_probe_is_the_persons_words_not_an_attachment_path():
    prompt = "/Users/x/.canopy/attachments/a.png\nplease look at this screenshot and tell me"
    assert delivery.probe_of(prompt).startswith("please look")


def test_silence_is_missing():
    class R:
        def read_new(self):
            return []
    t = [0.0]
    assert delivery.confirm(R(), "hi", timeout=1, poll=0.5,
                            clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s)) == delivery.MISSING


# ── wiring: the chat reuse path (the incident's path) ───────────────────────

class _Client:
    def __init__(self):
        self.events, self.failed, self.finished = [], None, None

    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "c-hal-thread"}

    def start(self, *a, **k):
        pass

    def record_session(self, *a, **k):
        pass

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        self.finished = note

    def fail_turn(self, turn_id, note):
        self.failed = note


def _cfg():
    tmp = Path(tempfile.mkdtemp(prefix="canopy-runner-test-"))
    return SimpleNamespace(cdp_port=9222, runner_id="r-1", emdash_db="/fake/emdash.db",
                           state_path=str(tmp / "runner-state.json"))


def _chat_turn(prompt="I just realized something else, for Sarvesh himself"):
    return {"id": "t1", "agent_slug": "hal", "project": "", "workspace_slug": "connect",
            "prompt": prompt, "origin_ref": {"chat_session_id": "s1", "thread_key": "s1"}}


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    path = tmp_path / "sess.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"content": "earlier reply",
                                                                 "stop_reason": "end_turn"}}) + "\n")
    monkeypatch.setattr(execute, "_resolve_transcript_path", lambda *a, **k: path)
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: path)
    return path


def _append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_a_chat_send_that_never_lands_fails_the_turn_and_says_so(monkeypatch, transcript):
    monkeypatch.setattr(cdp_control, "open_and_send", lambda *a, **k: {"ok": True, "action": "sent"})
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out == "failed:t1"
    assert client.failed and "not delivered" in client.failed and "send your message again" in client.failed
    assert any(e["payload"].get("status") == "undelivered" for e in client.events)
    assert "t1" not in chat_bridge.IN_FLIGHT        # no bridge waiting on a reply that can't come


def test_a_chat_send_that_lands_bridges_from_before_the_send(monkeypatch, transcript):
    def send(task, text, **k):
        _append(transcript, _user(text))
        _append(transcript, {"type": "assistant", "message": {"content": "on it", "stop_reason": "end_turn"}})
        return {"ok": True, "action": "sent"}
    monkeypatch.setattr(cdp_control, "open_and_send", send)
    client = _Client()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert out.startswith("chat:t1")
    assert client.failed is None
    bridge = chat_bridge.IN_FLIGHT["t1"]
    # The verifier read past the reply's first record; the bridge must still see it.
    bridge.step(bridge.reader.read_new())
    assert bridge.collected == ["on it"]


def test_an_unresolvable_transcript_sends_unverified_as_before(monkeypatch):
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
    monkeypatch.setattr(cdp_control, "open_and_send", lambda *a, **k: {"ok": True, "action": "sent"})
    client = _Client()

    execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert client.failed is None
    assert any(e["payload"].get("status") == "delivery_unverified" for e in client.events)


# ── wiring: the agent-turn reuse path ──────────────────────────────────────

class _AgentClient(_Client):
    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "c-hal-thread", "summary": ""}


def test_an_agent_turn_send_that_never_lands_fails_instead_of_reporting_reused(monkeypatch, transcript):
    monkeypatch.setattr(cdp_control, "open_and_send", lambda *a, **k: {"ok": True, "action": "sent"})
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: pytest.fail("never duplicate"))
    client = _AgentClient()

    out = execute.execute_turn(_cfg(), client, "r-1",
                               {"id": "t2", "agent_slug": "hal", "origin_ref": {"thread_id": "x"},
                                "prompt": "/hal:turn --thread x"})

    assert out == "failed:t2"
    assert client.failed and "not delivered" in client.failed
    assert client.finished is None
