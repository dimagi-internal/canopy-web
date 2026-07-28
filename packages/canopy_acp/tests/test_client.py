"""Transport tests.

The connection is split from process spawning precisely so these run against a
pair of pipes rather than a Node subprocess: the JSON-RPC rules (correlation,
concurrent in-flight requests, agent->client requests, malformed lines) are
where the bugs live, and none of them need a real agent.

The one thing that DOES need a real agent — that claude-agent-acp speaks what we
think it speaks — is in test_live_adapter.py, which skips when it isn't
installed.
"""
import io
import json
import threading

import pytest

from canopy_acp.client import AcpConnection, PermissionDecision


class FakeAgent:
    """The far end of the pipe: reads our requests, replies on demand."""

    def __init__(self):
        self.to_agent_r, self.to_agent_w = _pipe()
        self.from_agent_r, self.from_agent_w = _pipe()
        self.received = []

    def read_request(self, timeout=2.0):
        line = _readline(self.to_agent_r, timeout)
        msg = json.loads(line)
        self.received.append(msg)
        return msg

    def reply(self, request_id, result):
        self.from_agent_w.write(json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n")
        self.from_agent_w.flush()

    def error(self, request_id, message, code=-32000):
        self.from_agent_w.write(json.dumps(
            {"jsonrpc": "2.0", "id": request_id,
             "error": {"code": code, "message": message}}) + "\n")
        self.from_agent_w.flush()

    def notify(self, method, params):
        self.from_agent_w.write(json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.from_agent_w.flush()

    def request(self, request_id, method, params):
        """The agent asking US something (permissions, fs reads)."""
        self.from_agent_w.write(json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.from_agent_w.flush()

    def raw(self, text):
        self.from_agent_w.write(text + "\n")
        self.from_agent_w.flush()


def _pipe():
    import os
    r, w = os.pipe()
    return os.fdopen(r, "r", buffering=1), os.fdopen(w, "w", buffering=1)


def _readline(stream, timeout):
    result = []
    t = threading.Thread(target=lambda: result.append(stream.readline()), daemon=True)
    t.start()
    t.join(timeout)
    if not result:
        raise AssertionError("no line from the connection within timeout")
    return result[0]


@pytest.fixture
def wired():
    agent = FakeAgent()
    conn = AcpConnection(write_to=agent.to_agent_w, read_from=agent.from_agent_r)
    conn.start()
    yield conn, agent
    conn.close()


def test_a_request_resolves_with_its_own_reply(wired):
    conn, agent = wired
    pending = conn.request("initialize", {"protocolVersion": 1})
    req = agent.read_request()
    assert req["method"] == "initialize"
    agent.reply(req["id"], {"protocolVersion": 1, "authMethods": []})
    assert pending.result(timeout=2)["authMethods"] == []


def test_replies_are_matched_by_id_not_by_arrival_order(wired):
    """Two prompts can be in flight at once (steering), so out-of-order
    completion is normal rather than exotic."""
    conn, agent = wired
    first = conn.request("session/prompt", {"sessionId": "s", "prompt": "one"})
    second = conn.request("session/prompt", {"sessionId": "s", "prompt": "two"})
    req1 = agent.read_request()
    req2 = agent.read_request()
    agent.reply(req2["id"], {"stopReason": "second_done"})
    agent.reply(req1["id"], {"stopReason": "first_done"})
    assert second.result(timeout=2)["stopReason"] == "second_done"
    assert first.result(timeout=2)["stopReason"] == "first_done"


def test_an_error_reply_raises_on_the_waiting_caller(wired):
    conn, agent = wired
    pending = conn.request("session/load", {"sessionId": "nope"})
    req = agent.read_request()
    agent.error(req["id"], "no such session")
    with pytest.raises(RuntimeError, match="no such session"):
        pending.result(timeout=2)


def test_session_updates_reach_the_handler(wired):
    conn, agent = wired
    seen = []
    conn.on_update = lambda session_id, update: seen.append((session_id, update))
    agent.notify("session/update", {"sessionId": "s1",
                                    "update": {"sessionUpdate": "agent_message_chunk",
                                               "content": {"type": "text", "text": "hi"}}})
    _wait(lambda: seen)
    assert seen[0][0] == "s1"
    assert seen[0][1]["content"]["text"] == "hi"


def test_a_permission_request_is_answered_by_the_policy(wired):
    """The agent BLOCKS on this one — an unanswered permission request hangs the
    turn forever, which is why there is always a policy and never a default of
    silence."""
    conn, agent = wired
    conn.permission_policy = lambda params: PermissionDecision(
        params["options"][0]["optionId"])
    agent.request(99, "session/request_permission",
                  {"sessionId": "s", "options": [{"optionId": "allow", "kind": "allow_always"},
                                                 {"optionId": "deny", "kind": "reject_once"}]})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["id"] == 99
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "allow"}


def test_a_refused_permission_is_still_an_answer(wired):
    conn, agent = wired
    conn.permission_policy = lambda params: PermissionDecision(None)
    agent.request(7, "session/request_permission",
                  {"sessionId": "s", "options": [{"optionId": "allow", "kind": "allow_always"}]})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["result"]["outcome"] == {"outcome": "cancelled"}


def test_fs_reads_are_served_from_disk(tmp_path, wired):
    conn, agent = wired
    target = tmp_path / "note.txt"
    target.write_text("contents here")
    agent.request(5, "fs/read_text_file", {"path": str(target)})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["result"]["content"] == "contents here"


def test_an_fs_read_of_a_missing_file_errors_rather_than_hangs(wired, tmp_path):
    conn, agent = wired
    agent.request(6, "fs/read_text_file", {"path": str(tmp_path / "nope.txt")})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert "error" in reply


def test_an_unknown_agent_request_is_answered_not_ignored(wired):
    """Anything the agent BLOCKS on must get a response even when we don't
    implement it, or one unrecognised method wedges the session."""
    conn, agent = wired
    agent.request(11, "terminal/create", {"command": "ls"})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["id"] == 11
    assert "error" in reply


def test_a_malformed_line_does_not_kill_the_reader(wired):
    conn, agent = wired
    seen = []
    conn.on_update = lambda session_id, update: seen.append(update)
    agent.raw("this is not json {{{")
    agent.notify("session/update", {"sessionId": "s",
                                    "update": {"sessionUpdate": "agent_message_chunk",
                                               "content": {"type": "text", "text": "still here"}}})
    _wait(lambda: seen)
    assert seen[0]["content"]["text"] == "still here"


def test_a_handler_that_raises_does_not_kill_the_reader(wired):
    """An update handler is runner code posting over HTTP; it will fail
    sometimes, and observability may never cost a turn."""
    conn, agent = wired
    calls = []

    def explode(session_id, update):
        calls.append(update)
        raise ValueError("handler blew up")

    conn.on_update = explode
    agent.notify("session/update", {"sessionId": "s", "update": {"sessionUpdate": "x"}})
    _wait(lambda: calls)
    agent.notify("session/update", {"sessionId": "s", "update": {"sessionUpdate": "y"}})
    _wait(lambda: len(calls) >= 2)
    assert len(calls) == 2


def test_closing_fails_pending_requests_instead_of_hanging(wired):
    """A dead adapter must surface as an error on the waiter. Blocking forever
    on a process that exited is how a runner wedges with a turn EXECUTING."""
    conn, agent = wired
    pending = conn.request("session/prompt", {"sessionId": "s"})
    conn.close()
    with pytest.raises(RuntimeError):
        pending.result(timeout=2)


def test_notifications_carry_no_id(wired):
    """session/cancel is a notification: sending it as a request would wait for
    a reply that never comes."""
    conn, agent = wired
    conn.notify("session/cancel", {"sessionId": "s"})
    msg = agent.read_request()
    assert "id" not in msg
    assert msg["method"] == "session/cancel"


def _wait(predicate, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")
