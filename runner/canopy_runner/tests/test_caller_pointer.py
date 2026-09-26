"""Every prompt a canopy turn delivers leaves the plugin's UserPromptSubmit hook a
pointer to who is asking — keyed by the emdash task, written BEFORE the text lands,
withdrawn when the text did not land, and never inside the person's words."""
import json
import os
import stat
import types

import pytest

from canopy_runner import caller, cdp_control, chat_bridge, emdash, execute

TID = "3f2b8c1e-0000-4000-8000-000000000001"
ENV = {"version": 1, "turn_id": TID, "relationship": "caller", "verified": False,
       "who": {"kind": "contact", "contact": {"email": "x@partner.org", "name": "X"}},
       "profile": "full", "turn_mode": {"mode": "manual", "basis": "agent"}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(caller, "CALLER_ROOT", tmp_path / "caller")
    monkeypatch.setattr(caller, "PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    chat_bridge.IN_FLIGHT.clear()
    yield tmp_path
    chat_bridge.IN_FLIGHT.clear()


def _pending(tmp_path, task):
    p = tmp_path / "caller" / "pending" / f"{task}.json"
    return json.loads(p.read_text()) if p.exists() else None


# --- the pointer itself --------------------------------------------------------------

def test_the_pointer_names_the_turn_and_its_envelope_privately(tmp_path):
    env = caller.write_caller_file({"id": TID, "caller_context": ENV})
    p = caller.write_pending("c-slack-4a4e", {"id": TID}, env, now=lambda: 1000.0)
    doc = json.loads(p.read_text())
    assert doc == {"version": 1, "turn_id": TID, "task": "c-slack-4a4e",
                   "envelope": str(env), "written_at": 1000.0}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700


def test_no_envelope_no_pointer(tmp_path):
    assert caller.write_pending("c-x-4a4e", {"id": TID}, None) is None
    assert not (tmp_path / "caller" / "pending").exists()


@pytest.mark.parametrize("task", ["../escape", "", "a/b", ".hidden"])
def test_a_task_name_is_never_joined_onto_a_path_unless_it_is_a_name(tmp_path, task):
    assert caller.write_pending(task, {"id": TID}, tmp_path / "e.json") is None


def test_clear_withdraws_it_and_never_raises(tmp_path):
    caller.write_pending("c-x-4a4e", {"id": TID}, tmp_path / "e.json")
    caller.clear_pending("c-x-4a4e")
    caller.clear_pending("c-x-4a4e")          # already gone: still fine
    assert _pending(tmp_path, "c-x-4a4e") is None


# --- a free-text chat turn -----------------------------------------------------------

def _chat_turn(**kw):
    d = {"id": TID, "agent_slug": "hal", "project": "", "workspace_slug": "connect",
         "prompt": "please push and deploy", "caller_context": ENV,
         "origin_ref": {"chat_session_id": "s1", "thread_key": "slack:C1:1.2"}}
    d.update(kw)
    return d


class _ChatClient:
    def __init__(self, plan):
        self.plan, self.failed, self.events = plan, None, []

    def resolve_session(self, *a, **k):
        return dict(self.plan)

    def start(self, *a, **k):
        pass

    def record_session(self, *a, **k):
        pass

    def post_events(self, turn_id, evs):
        self.events.extend(evs)

    def finish(self, *a, **k):
        pass

    def fail_turn(self, turn_id, note):
        self.failed = note


_CFG = types.SimpleNamespace(cdp_port=9222, emdash_db="/nonexistent")


def test_a_chat_reuse_leaves_the_pointer_before_the_words_and_leaves_the_words_alone(
        monkeypatch, _isolated):
    seen = {}

    def send(task, text, port=9222, **k):
        seen.update(text=text, pointer=_pending(_isolated, task))
        return {"ok": True, "action": "sent"}

    monkeypatch.setattr(cdp_control, "open_and_send", send)
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
    execute.execute_chat_turn(_CFG, _ChatClient({"reuse": True, "emdash_task_id": "c-slack-9a1b"}),
                              "r1", _chat_turn())
    assert seen["text"] == "please push and deploy"          # byte-for-byte theirs
    assert seen["pointer"]["turn_id"] == TID
    assert seen["pointer"]["envelope"].endswith(f"{TID}.json")


def test_a_new_chat_session_gets_the_pointer_under_the_name_it_asked_for(monkeypatch, _isolated):
    seen = {}

    def create(target, prompt, task_name, port):
        seen["pointer"] = _pending(_isolated, task_name)
        return {"task": task_name}

    monkeypatch.setattr(cdp_control, "create_task", create)
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
    execute.execute_chat_turn(_CFG, _ChatClient({"reuse": False}), "r1", _chat_turn())
    assert seen["pointer"]["turn_id"] == TID


def test_a_renamed_session_moves_the_pointer_to_the_real_name(monkeypatch, _isolated):
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda target, prompt, task_name, port: {"task": task_name + "-2"})
    monkeypatch.setattr(execute, "_wait_for_transcript", lambda *a, **k: None)
    execute.execute_chat_turn(_CFG, _ChatClient({"reuse": False}), "r1", _chat_turn())
    names = [p.stem for p in (_isolated / "caller" / "pending").glob("*.json")]
    assert len(names) == 1 and names[0].endswith("-2")


def test_a_send_that_failed_withdraws_the_pointer(monkeypatch, _isolated):
    """Otherwise the next thing a HUMAN types there is attributed to the caller."""
    def boom(*a, **k):
        raise cdp_control.CDPError("COMPOSER_NOT_VISIBLE")

    monkeypatch.setattr(cdp_control, "open_and_send", boom)
    monkeypatch.setattr(execute.hooks, "read_hook_menu_from", lambda *a, **k: None)
    client = _ChatClient({"reuse": True, "emdash_task_id": "c-slack-9a1b"})
    assert execute.execute_chat_turn(_CFG, client, "r1", _chat_turn()).startswith("failed:")
    assert _pending(_isolated, "c-slack-9a1b") is None


def test_a_collision_that_delivered_nothing_withdraws_the_pointer(monkeypatch, _isolated):
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda *a, **k: {"ok": True, "action": "collision", "line": "half"})
    monkeypatch.setattr(execute.dialog, "collision_choice", lambda *a, **k: execute.dialog.NEW)
    execute._COLLISION_ANSWERS.clear()
    execute.execute_chat_turn(_CFG, _ChatClient({"reuse": True, "emdash_task_id": "c-slack-9a1b"}),
                              "r1", _chat_turn())
    assert _pending(_isolated, "c-slack-9a1b") is None


# --- an agent turn (command-shaped, or a repo brief) ----------------------------------

def test_an_agent_turn_gets_the_pointer_as_well_as_the_flag(monkeypatch, _isolated):
    from tests.test_execute import FakeClient, _cfg

    seen = {}

    def create(agent, prompt, task_name, port):
        seen.update(prompt=prompt, pointer=_pending(_isolated, task_name))
        return {"task": task_name}

    monkeypatch.setattr(cdp_control, "create_task", create)
    turn = {"id": TID, "agent_slug": "hal", "origin_ref": {}, "prompt": "/hal:turn",
            "caller_context": ENV}
    execute.execute_turn(_cfg(), FakeClient({"reuse": False}), "r1", turn)
    assert " --caller " in seen["prompt"]
    assert seen["pointer"]["turn_id"] == TID
