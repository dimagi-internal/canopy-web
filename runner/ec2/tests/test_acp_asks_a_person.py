"""A cloud chat's agent can ask the person, and the person's tap reaches it.

2026-10-19, cloud-ec2-2: the live e2e's `answer_from_the_web` timed out — the
agent was told to use AskUserQuestion and no dialog ever reached the web. The
ACP client never advertised form elicitation, so claude-agent-acp disabled the
tool outright (`disallowedTools`), and every permission request was answered
"allow" by policy without anyone seeing it.

Now a chat turn's agent is built with someone to ask: the ask is posted to the
session's binding (`/session-menu`), the answer is drained from `/menu-answers`
— the same durable record a laptop's tap rides — and retired with its outcome.
A turn with no chat behind it keeps the unattended policy: nobody would see it.
"""
from __future__ import annotations

import threading

import pytest

TID = "7e1f0c2a-0000-4000-8000-000000000001"
SID = "5d0e9b1c-0000-4000-8000-000000000002"
RID = "c6332c59-54b2-4943-a65a-538c47bf6329"

ASK = {"mode": "form", "sessionId": "acp-s", "toolCallId": "toolu_1", "message": "Pick a colour",
       "requestedSchema": {"type": "object", "properties": {
           "question_0": {"type": "string", "oneOf": [{"const": "Aubergine", "title": "Aubergine"},
                                                      {"const": "Beige", "title": "Beige"}]},
           "question_0_custom": {"type": "string", "title": "Other"}}}}

PERMISSION = {"sessionId": "acp-s",
              "options": [{"kind": "allow_once", "name": "Allow", "optionId": "allow"},
                          {"kind": "reject_once", "name": "Reject", "optionId": "reject"}],
              "toolCall": {"toolCallId": "toolu_2", "kind": "execute", "title": "rm x",
                           "rawInput": {"command": "rm x"}}}


class FakeServer:
    """canopy-web's three runner routes, as the box sees them."""

    def __init__(self, *, accept_menu=True):
        self.accept_menu = accept_menu
        self.menus = []          # every menu posted (None = cleared)
        self.answers = []        # what /menu-answers will list
        self.results = []        # (answer_id, outcome)
        self.posted = threading.Event()

    def api(self, method, path, body=None, **_):
        if path.endswith("/session-menu"):
            self.menus.append(body["menu"])
            if body["menu"] is not None:
                self.posted.set()
            return 200, {"ok": self.accept_menu}
        if path.endswith("/menu-answers"):
            return 200, {"answers": list(self.answers)}
        if path.endswith("/menu-answer-result"):
            self.results.append((body["answer_id"], body["outcome"]))
            self.answers = [a for a in self.answers if a["answer_id"] != body["answer_id"]]
            return 200, {"ok": True}
        raise AssertionError(f"unexpected {method} {path}")

    def tap(self, answer_id="a1", option=1, selections=None, texts=None):
        self.answers.append({"session_id": SID, "session_key": "k", "answer_id": answer_id,
                             "option": option, "selections": selections, "texts": texts})


@pytest.fixture
def box(cloud_runner, monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(cloud_runner, "_api", server.api)
    monkeypatch.setattr(cloud_runner, "MENU_ANSWER_POLL_SECONDS", 0.02)
    with cloud_runner._TURNS_LOCK:
        cloud_runner._IN_FLIGHT[TID] = None
    yield cloud_runner, server
    with cloud_runner._TURNS_LOCK:
        cloud_runner._IN_FLIGHT.pop(TID, None)
    cloud_runner._clear_stop(TID)


def _ask_in_background(mod, method, params):
    ask = mod._ask_a_person(RID, TID, SID)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("result", ask(method, params)), daemon=True)
    t.start()
    return t, out


def test_a_chat_turn_gets_an_agent_that_can_ask_and_a_headless_one_does_not(box):
    mod, _server = box
    assert mod._ask_human_for_turn(RID, TID, SID) is not None
    assert mod._ask_human_for_turn(RID, TID, "") is None


def test_the_ask_reaches_the_session_and_the_tap_reaches_the_agent(box):
    mod, server = box
    t, out = _ask_in_background(mod, "elicitation/create", ASK)
    assert server.posted.wait(2), "the dialog never reached canopy-web"
    posted = server.menus[0]
    assert [o["label"] for o in posted["options"]] == ["Aubergine", "Beige"]
    assert posted["source"] == "acp" and posted["observed_at"]

    server.tap(option=1)
    t.join(2)
    assert out["result"] == {"action": "accept", "content": {"question_0": "Aubergine"}}
    assert server.results == [("a1", "answered")], "the answer was never retired"
    assert server.menus[-1] is None, "the dialog stayed up after it was answered"


def test_a_permission_prompt_is_the_person_s_call_too(box):
    mod, server = box
    t, out = _ask_in_background(mod, "session/request_permission", PERMISSION)
    assert server.posted.wait(2)
    assert server.menus[0]["body"] == "rm x"
    server.tap(option=2)
    t.join(2)
    assert out["result"] == {"outcome": {"outcome": "selected", "optionId": "reject"}}


def test_a_tap_the_dialog_never_offered_is_refused_and_the_ask_stays_open(box):
    mod, server = box
    t, out = _ask_in_background(mod, "elicitation/create", ASK)
    assert server.posted.wait(2)
    server.tap(answer_id="bad", option=9)
    _wait(lambda: ("bad", "not_on_menu") in server.results)
    assert t.is_alive(), "an answer the agent never offered ended the ask"
    assert any(m and m.get("answer_error") == "not_on_menu" for m in server.menus), (
        "the refusal never reached the person who tapped")
    server.tap(answer_id="good", option=2)
    t.join(2)
    assert out["result"]["content"] == {"question_0": "Beige"}


def test_a_stop_ends_the_wait(box):
    mod, server = box
    t, out = _ask_in_background(mod, "elicitation/create", ASK)
    assert server.posted.wait(2)
    mod.request_stop(TID, "test")
    t.join(2)
    assert out["result"] == {"action": "cancel"}
    assert server.menus[-1] is None


def test_when_nobody_can_be_shown_the_ask_it_is_answered_as_unattended(box, monkeypatch):
    """An older canopy-web (no route) or a session this box does not hold: blocking
    on a dialog nobody can see is the one outcome worse than the old policy."""
    mod, server = box
    server.accept_menu = False
    ask = mod._ask_a_person(RID, TID, SID)
    assert ask("elicitation/create", ASK) == {"action": "decline"}
    assert ask("session/request_permission", PERMISSION) == {
        "outcome": {"outcome": "selected", "optionId": "allow"}}


def test_an_answer_with_no_ask_waiting_here_is_retired(box):
    """The ask ended (answered at another surface, or its turn finished) while a
    tap was in flight. Left set, it would be re-listed forever."""
    mod, server = box
    server.tap(answer_id="late", option=1)
    mod._drain_menu_answers(RID)
    assert server.results == [("late", "no_dialog")]


def test_the_doorbell_frame_drains_at_once(box):
    mod, server = box
    t, out = _ask_in_background(mod, "elicitation/create", ASK)
    assert server.posted.wait(2)
    server.tap(option=2)
    mod._ring_menu_answers(RID)
    t.join(2)
    assert out["result"]["content"] == {"question_0": "Beige"}


def _built_with(mod, monkeypatch, tmp_path, chat_session):
    """The kwargs run_acp builds this turn's agent with."""
    built = {}

    class Agent:
        session_id = "acp-t"

        def __init__(self, **kwargs):
            built.update(kwargs)

        def start(self):
            pass

        def new_session(self, timeout=None):
            return self.session_id

        def prompt(self, _p):
            class Done:
                def result(self, timeout=None):
                    return {"stopReason": "end_turn"}
            return Done()

        def cancel(self):
            pass

        def close(self):
            pass

    class Reducer:
        assistant_text, rate_limit = "done", None

        def apply(self, update):
            return None

        def reset_stream_state(self):
            pass

    class Core:
        AcpAgent = Agent
        UpdateReducer = Reducer

    monkeypatch.setattr(mod, "_acp_core", lambda: Core)
    monkeypatch.setattr(mod, "_agent_env", lambda slug: {})
    monkeypatch.setattr(mod, "_RUNNER_ID", RID)
    mod._TURN_ENV.chat_session = chat_session
    try:
        ok, note, _ = mod.run_acp("hi", TID, lambda e: None, cwd=tmp_path)
    finally:
        mod._TURN_ENV.chat_session = ""
    assert ok, note
    return built


def test_run_acp_gives_a_chat_turn_s_agent_someone_to_ask(box, monkeypatch, tmp_path):
    mod, _server = box
    assert callable(_built_with(mod, monkeypatch, tmp_path, SID).get("ask_human"))


def test_run_acp_leaves_a_headless_turn_unattended(box, monkeypatch, tmp_path):
    mod, _server = box
    assert "ask_human" not in _built_with(mod, monkeypatch, tmp_path, "")


def _wait(predicate, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
