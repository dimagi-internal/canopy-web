"""execute_chat_turn wiring: a chat turn injects into emdash (mocked) and REGISTERS a
bridge that the tick pump (see test_chat_pump.py) carries to completion."""
import types

import pytest

from canopy_runner import chat_bridge, execute

from .test_menu import BUSY, PERMISSION


class _FakeClient:
    def __init__(self):
        self.events = []
        self.finished = None
        self.finished_task = None
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

    def finish(self, turn_id, note="", status="done", emdash_task_id=""):
        self.finished = note
        self.finished_status = status
        self.finished_task = emdash_task_id

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


# -- a send that bounces off a dialog must say WHAT is being asked ------------

class _ReuseClient(_FakeClient):
    """A chat client whose session already has an emdash task to reuse."""

    def __init__(self):
        super().__init__()
        self.streamed = []

    def resolve_session(self, *a, **k):
        return {"reuse": True, "emdash_task_id": "ada-chat-1435"}

    def post_session_stream(self, runner_id, session_id, events):
        self.streamed.append({"session_id": session_id, "events": events})


def _bounce_off_a_dialog(monkeypatch, screen):
    """A reuse send that fails COMPOSER_NOT_VISIBLE, with `screen` on the terminal."""
    def _no_composer(task, text, clear_first=False, port=9222):
        raise execute.cdp_control.CDPError(
            'COMPOSER_NOT_VISIBLE: no input line in the rendered frame for '
            f'task "{task}" (mid-redraw, a menu is up, or a stale frame) '
            '— refusing a blind send')

    monkeypatch.setattr(execute.cdp_control, "open_and_send", _no_composer)
    monkeypatch.setattr(execute.cdp_control, "read_terminal", lambda task, port=9222: screen)
    monkeypatch.setattr(execute.emdash, "task_state", lambda *a, **k: "open")
    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _ReuseClient()
    res = execute.execute_chat_turn(cfg, client, "runner1", _turn())
    return res, client


def test_composer_not_visible_ships_the_dialog_the_agent_is_waiting_on(monkeypatch):
    """The composer is missing BECAUSE a dialog is where the input line should be.

    `open-send` has already opened that task, so it is the active terminal and the
    menu is free to read — no focus steal, which is what rules reading one out on a
    hook signal. Without this the phone gets a bare failed turn: no question, no
    buttons, and no way to tell "the agent asked you something" from "the send
    broke". That is the silence that cost 2026-07-30 two hours.
    """
    res, client = _bounce_off_a_dialog(monkeypatch, PERMISSION)

    assert res.startswith("failed:")
    blocked = [e for s in client.streamed for e in s["events"]
               if e["kind"] == "activity:blocked"]
    assert blocked, "the phone was never told the agent is waiting on a dialog"
    menu = blocked[0]["payload"]["menu"]
    assert menu["question"] == "Do you want to proceed?"
    assert [o["label"] for o in menu["options"]][0] == "Yes"
    assert client.streamed[0]["session_id"] == "s1"


def test_a_bounced_send_says_the_agent_is_waiting_not_just_the_cdp_error(monkeypatch):
    """`chat reuse send failed: COMPOSER_NOT_VISIBLE...` is a runner-internal
    string. The human needs the actionable half: their message did not land
    because the agent is waiting on a question."""
    _res, client = _bounce_off_a_dialog(monkeypatch, PERMISSION)

    assert "waiting" in (client.failed or "").lower()


def test_a_bounce_with_no_dialog_on_screen_stays_a_plain_failure(monkeypatch):
    """Mid-redraw and stale frames hit the same error with NO menu. Reporting
    "blocked" there would tell the phone an agent needs a human when it is merely
    busy — the negative case `menu.BUSY` exists to protect."""
    res, client = _bounce_off_a_dialog(monkeypatch, BUSY)

    assert res.startswith("failed:")
    assert not [e for s in client.streamed for e in s["events"]
                if e["kind"] == "activity:blocked"]


def test_a_stop_before_delivery_sends_nothing_at_all(monkeypatch, tmp_path):
    """The cheapest cancel there is: the human pressed stop between the claim and the
    send, so the message must never reach the agent's session.

    `cancel_check` used to be "accepted for signature compatibility and is no longer
    read here", which made this cancel impossible — the only consumer of the signal
    was the pump, and at this point there is no bridge for it to pump. The message was
    typed into the agent regardless, and the turn then ran to completion."""
    sent = []
    monkeypatch.setattr(execute.cdp_control, "create_task",
                        lambda *a, **k: sent.append(a) or {"task": "echo-1234"})
    monkeypatch.setattr(execute.cdp_control, "open_and_send",
                        lambda *a, **k: sent.append(a) or {"action": "sent"})

    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    client = _FakeClient()

    res = execute.execute_chat_turn(cfg, client, "runner1", _turn(),
                                    cancel_check=lambda tid: tid == "t1")

    assert res == "cancelled:t1"
    assert sent == [], "nothing may be delivered into the agent's session"
    assert client.finished_status == "cancelled"
    assert chat_bridge.IN_FLIGHT == {}, "no bridge to pump — the turn is already over"


# -- a requeued chat turn must not re-open the same collision popup ------------

@pytest.fixture()
def _fresh_collision_answers():
    execute._COLLISION_ANSWERS.clear()
    yield
    execute._COLLISION_ANSWERS.clear()


def _requeued_into_collision(monkeypatch, choice, lines):
    """Run one chat turn per `lines` entry into a prompt holding that unsent line —
    the shape of a deferred turn the server requeues and this runner re-claims."""
    asked = []
    lines = list(lines)

    def fake_open_and_send(task, text, clear_first=False, port=9222):
        if clear_first:
            return {"ok": True, "action": "sent-cleared", "task": task}
        return {"ok": True, "action": "collision", "task": task, "line": lines[0]}

    def fake_choice(task, line, **k):
        asked.append(line)
        return choice

    monkeypatch.setattr(execute.cdp_control, "open_and_send", fake_open_and_send)
    monkeypatch.setattr(execute.dialog, "collision_choice", fake_choice)
    monkeypatch.setattr(execute.emdash, "task_state", lambda *a, **k: "open")
    cfg = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")
    results = []
    while lines:
        client = _ReuseClient()
        results.append((execute.execute_chat_turn(cfg, client, "runner1", _turn()), client))
        lines.pop(0)
    return asked, results


def test_a_requeued_turn_does_not_ask_again_about_the_same_text(
    monkeypatch, _fresh_collision_answers
):
    """2026-09-18: one deferred chat turn re-claimed 4 times put up 4 thirty-second
    popups, and nothing else on the box could be claimed while each was up."""
    asked, results = _requeued_into_collision(
        monkeypatch, execute.dialog.NEW, ["half typed"] * 4
    )
    assert asked == ["half typed"]  # one popup, not four
    for res, client in results:
        assert res.startswith("deferred:")  # still deferred every time, never sent
        assert "unsent text" in client.failed


def test_changed_text_is_a_new_question(monkeypatch, _fresh_collision_answers):
    asked, _ = _requeued_into_collision(
        monkeypatch, execute.dialog.NEW, ["half typed", "half typed and more"]
    )
    assert asked == ["half typed", "half typed and more"]


def test_clear_is_never_reused_without_asking(monkeypatch, _fresh_collision_answers):
    """Clear deletes the human's text; each deletion needs its own yes."""
    asked, _ = _requeued_into_collision(
        monkeypatch, execute.dialog.CLEAR, ["half typed"] * 2
    )
    assert asked == ["half typed", "half typed"]


def test_a_remembered_answer_expires(monkeypatch, _fresh_collision_answers):
    monkeypatch.setattr(execute.dialog, "collision_choice", lambda *a, **k: execute.dialog.NEW)
    assert execute._chat_collision_choice("t", "x", now_fn=lambda: 0.0) == execute.dialog.NEW
    assert ("t", "x") in execute._COLLISION_ANSWERS
    execute._chat_collision_choice("t", "y", now_fn=lambda: execute.COLLISION_ANSWER_TTL + 1)
    assert ("t", "x") not in execute._COLLISION_ANSWERS


def test_the_undelivered_note_tells_the_person_what_to_do(
    monkeypatch, _fresh_collision_answers
):
    """The note is the whole explanation a person gets: apps/slack/relay.py posts a
    failed turn's result_note into the thread. It must name the session and the fix,
    not an internal task id on its own."""
    _, results = _requeued_into_collision(monkeypatch, execute.dialog.NEW, ["half typed"])
    note = results[0][1].failed
    assert "ada-chat-1435" in note          # WHICH session is in the way
    assert "unsent text" in note            # WHY it could not be delivered
    assert "send your message again" in note  # WHAT to do about it
    assert "deferred" not in note           # not runner jargon
