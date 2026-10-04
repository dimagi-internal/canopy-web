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
        self.finished_status, self.finished_task = None, None

    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "c-hal-thread"}

    def start(self, *a, **k):
        pass

    def record_session(self, *a, **k):
        pass

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        # A FAILED finish is recorded as `failed` too, so assertions read the same
        # whichever call the runner used; `finished_task` is what stops a requeue.
        self.finished_status, self.finished_task = status, emdash_task_id
        if status == "failed":
            self.failed = note
        else:
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
    # It cannot know the message was lost — only that it could not see it land.
    assert client.failed and "Couldn't confirm" in client.failed
    assert "not delivered" not in client.failed
    assert "send your message again" not in client.failed
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
    assert client.failed and "Couldn't confirm" in client.failed
    assert client.finished is None


# ── turn 22662f53 (2026-10-04): two sessions named "editing" ───────────────
#
# eva's chat sent "No can leave it.  Are wee good to close out?" to eva's emdash
# session "editing". ada ALSO had a session named "editing", listed above eva's in
# the sidebar, and the sidecar opened the FIRST row with that label — so the text
# went into ada's session. The verifier, correctly, watched EVA's transcript, saw
# nothing, and failed the turn with no session key; the server requeued it as a
# non-attempt; the runner typed it again. Four times in ada's session, zero in
# eva's, and the human was told it was never delivered.

FIXTURE = Path(__file__).parent / "fixtures" / "t52_prompt_landed.jsonl"
T52_PROMPT = "No can leave it.  Are wee good to close out?"


def _landed_records():
    return [json.loads(ln) for ln in FIXTURE.read_text().splitlines() if ln.strip()]


def test_the_real_landed_record_matches_the_message():
    """The matcher was NOT the fault: the record Claude Code wrote for this exact
    message (double space and all) confirms. What failed was WHICH session was
    typed into, relative to the transcript being watched."""
    assert delivery.classify(_landed_records(), T52_PROMPT) == delivery.CONFIRMED


@pytest.fixture
def two_editing_sessions(tmp_path, monkeypatch):
    """ada and eva each own an emdash task called "editing", each with its own
    transcript. Resolution is per (project, task), as in production."""
    paths = {}
    for project in ("ada", "eva"):
        p = tmp_path / f"{project}-editing.jsonl"
        p.write_text(json.dumps({"type": "assistant", "message": {
            "content": f"{project} earlier reply", "stop_reason": "end_turn"}}) + "\n")
        paths[project] = p
    monkeypatch.setattr(execute, "_resolve_transcript_path",
                        lambda target, task, **k: paths.get(target))
    monkeypatch.setattr(execute, "_wait_for_transcript",
                        lambda target, task, **k: paths.get(target))
    return paths


def _sidebar_send(paths, typed):
    """The sidecar's lookup, faithfully: with a project it opens that project's row;
    without one it opens the FIRST row with the label — ada's, which sits above
    eva's. Whatever it opens receives the records Claude Code really wrote."""
    sidebar_order = ["ada", "eva"]

    def send(task, text, clear_first=False, port=9222, project=""):
        opened = project if project else sidebar_order[0]
        typed.append(opened)
        with open(paths[opened], "a") as f:
            for rec in _landed_records():
                f.write(json.dumps(rec) + "\n")
        return {"ok": True, "action": "sent", "task": task}
    return send


def _eva_turn():
    return {"id": "22662f53", "agent_slug": "eva", "project": "", "workspace_slug": "dimagi",
            "prompt": T52_PROMPT,
            "origin_ref": {"chat_session_id": "d8ef09c5", "thread_key": "emdash:editing"}}


class _EvaClient(_Client):
    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "editing"}


def test_a_send_to_a_same_named_session_reaches_the_turns_own_project(
        monkeypatch, two_editing_sessions):
    typed = []
    monkeypatch.setattr(cdp_control, "open_and_send", _sidebar_send(two_editing_sessions, typed))
    client = _EvaClient()

    out = execute.execute_chat_turn(_cfg(), client, "r-1", _eva_turn())

    assert typed == ["eva"], "the message must go to eva's 'editing', not ada's"
    assert out.startswith("chat:22662f53")
    assert client.failed is None
    assert T52_PROMPT not in two_editing_sessions["ada"].read_text()


def test_an_unconfirmed_send_is_failed_with_its_session_so_it_is_never_requeued(
        monkeypatch, transcript):
    """The server requeues a FAILED turn that reports no session key, as proof that
    nothing reached an agent. Here the keystrokes DID go out — so the failure must
    name the session, which makes it terminal."""
    monkeypatch.setattr(cdp_control, "open_and_send", lambda *a, **k: {"ok": True, "action": "sent"})
    client = _Client()

    execute.execute_chat_turn(_cfg(), client, "r-1", _chat_turn())

    assert client.finished_status == "failed"
    assert client.finished_task == "c-hal-thread"


def test_a_reclaimed_turn_is_never_typed_twice(monkeypatch, transcript):
    """Even if the turn comes back (an older server's requeue, a lease reclaim), its
    message is typed at most once by this runner."""
    sends = []
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda *a, **k: sends.append(a) or {"ok": True, "action": "sent"})

    first, again = _Client(), _Client()
    execute.execute_chat_turn(_cfg(), first, "r-1", _chat_turn())
    out = execute.execute_chat_turn(_cfg(), again, "r-1", _chat_turn())

    assert len(sends) == 1
    assert out == "failed:t1"
    assert again.finished_status == "failed" and again.finished_task == "c-hal-thread"
    assert "Couldn't confirm" in again.failed
    assert any(e["payload"].get("status") == "not_retyped" for e in again.events)


def test_an_agent_turn_is_also_aimed_by_project(monkeypatch, two_editing_sessions):
    typed = []
    monkeypatch.setattr(cdp_control, "open_and_send", _sidebar_send(two_editing_sessions, typed))
    monkeypatch.setattr(cdp_control, "create_task", lambda *a, **k: pytest.fail("never duplicate"))

    class _C(_Client):
        def resolve_session(self, *a, **k):
            return {"reuse": True, "emdash_task_id": "editing", "summary": ""}
    client = _C()

    execute.execute_turn(_cfg(), client, "r-1",
                         {"id": "t3", "agent_slug": "eva", "origin_ref": {"thread_id": "x"},
                          "prompt": T52_PROMPT})

    assert typed == ["eva"]
