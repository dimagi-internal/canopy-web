"""Transport tests.

The connection is split from process spawning precisely so these run against a
pair of pipes rather than a Node subprocess: the JSON-RPC rules (correlation,
concurrent in-flight requests, agent->client requests, malformed lines) are
where the bugs live, and none of them need a real agent.

The one thing that DOES need a real agent — that claude-agent-acp speaks what we
think it speaks — is in test_live_adapter.py, which skips when it isn't
installed.
"""
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


ASK = {"mode": "form", "sessionId": "s", "toolCallId": "toolu_1", "message": "Pick a colour",
       "requestedSchema": {"type": "object", "properties": {
           "question_0": {"type": "string", "oneOf": [{"const": "Aubergine", "title": "Aubergine"},
                                                      {"const": "Beige", "title": "Beige"}]}}}}


def test_without_a_human_an_elicitation_is_refused_not_ignored(wired):
    """Nobody to ask: the agent still gets an answer, so the turn never wedges."""
    conn, agent = wired
    agent.request(21, "elicitation/create", ASK)
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["id"] == 21
    assert "error" in reply


def test_an_elicitation_is_answered_by_the_human_handler(wired):
    conn, agent = wired
    conn.ask_human = lambda method, params: {"action": "accept",
                                             "content": {"question_0": "Aubergine"}}
    agent.request(22, "elicitation/create", ASK)
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["id"] == 22
    assert reply["result"] == {"action": "accept", "content": {"question_0": "Aubergine"}}


def test_a_permission_request_goes_to_the_human_handler_when_there_is_one(wired):
    conn, agent = wired
    conn.ask_human = lambda method, params: {
        "outcome": {"outcome": "selected", "optionId": "deny"}} if method == \
        "session/request_permission" else None
    agent.request(23, "session/request_permission",
                  {"sessionId": "s", "options": [{"optionId": "allow", "kind": "allow_once"},
                                                 {"optionId": "deny", "kind": "reject_once"}]})
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["result"]["outcome"] == {"outcome": "selected", "optionId": "deny"}


def test_waiting_on_a_human_does_not_stall_the_reader(wired):
    """A person can take minutes to answer. Updates — and a cancel's reply —
    must keep flowing meanwhile, so the wait runs off the reader thread."""
    conn, agent = wired
    release = threading.Event()
    seen = []
    conn.on_update = lambda session_id, update: seen.append(update)

    def slow_human(method, params):
        release.wait(5)
        return {"action": "decline"}

    conn.ask_human = slow_human
    agent.request(24, "elicitation/create", ASK)
    agent.notify("session/update", {"sessionId": "s", "update": {"sessionUpdate": "z"}})
    _wait(lambda: seen)
    release.set()
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply == {"jsonrpc": "2.0", "id": 24, "result": {"action": "decline"}}


def test_a_human_handler_that_raises_still_answers(wired):
    conn, agent = wired

    def broken(method, params):
        raise RuntimeError("canopy-web unreachable")

    conn.ask_human = broken
    agent.request(25, "elicitation/create", ASK)
    reply = json.loads(_readline(agent.to_agent_r, 2))
    assert reply["id"] == 25
    assert "error" in reply


def test_form_elicitation_is_advertised_only_when_someone_can_answer(tmp_path):
    """The adapter disables AskUserQuestion outright unless the client says it
    can render a form, and a box with nobody to ask must keep it disabled — an
    unattended turn would otherwise block on a question no one sees."""
    from canopy_acp.client import AcpAgent

    headless = AcpAgent(cwd=tmp_path, argv=["true"]).initialize_params()
    assert "elicitation" not in headless["clientCapabilities"]
    asked = AcpAgent(cwd=tmp_path, argv=["true"],
                     ask_human=lambda method, params: {}).initialize_params()
    assert asked["clientCapabilities"]["elicitation"] == {"form": {}}
    assert asked["clientCapabilities"]["fs"] == headless["clientCapabilities"]["fs"]


def _wait(predicate, timeout=2.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_a_dead_adapter_says_why(tmp_path):
    """The adapter dying mid-turn surfaced only as "ACP connection closed before a
    reply arrived": its stderr went to a DEBUG log and its exit status nowhere, so
    an OOM kill, a crash and a clean exit all read the same (2026-10-05, a hal
    turn on cloud-ec2-1 that nobody could diagnose afterwards)."""
    import sys

    from canopy_acp.client import AcpAgent

    agent = AcpAgent(cwd=tmp_path, argv=[
        sys.executable, "-c",
        "import sys; sys.stderr.write('FATAL heap out of memory\\n'); sys.stderr.flush(); sys.exit(134)",
    ])
    with pytest.raises(RuntimeError):
        agent.start()
    report = agent.exit_report(wait=5)
    assert "exit code 134" in report
    assert "FATAL heap out of memory" in report
    agent.close()


def test_a_running_adapter_reports_no_exit(tmp_path):
    import sys

    from canopy_acp.client import AcpAgent

    agent = AcpAgent(cwd=tmp_path, argv=[sys.executable, "-c", "import time; time.sleep(30)"])
    agent.proc = None
    assert agent.exit_report() == ""


def test_the_adapter_pipes_are_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """On Windows the locale default is cp1252, which cannot decode the 0x9D byte
    of a right double quote. The reader died on it and the turn failed as "ACP
    connection closed before a reply arrived" (2026-10-26, fizzy email turn
    b50c9a50 on shayoni-win-cdp; failure investigation 11). Here the locale
    default is forced to cp1252, as on that box."""
    import subprocess
    import sys

    from canopy_acp.client import AcpAgent

    real = subprocess.Popen

    def windows_popen(*args, **kwargs):
        kwargs.setdefault("encoding", "cp1252")
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", windows_popen)
    reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentInfo": {"title": "a “quoted” reply"}}},
                       ensure_ascii=False)
    agent = AcpAgent(cwd=tmp_path, argv=[
        sys.executable, "-c",
        "import sys; sys.stdin.readline(); "
        f"sys.stdout.buffer.write({(reply + chr(10)).encode('utf-8')!r}); sys.stdout.flush(); "
        "sys.stdin.readline()",
    ])
    try:
        result = agent.start()
    finally:
        agent.close()
    assert result["agentInfo"]["title"] == "a “quoted” reply"


def test_a_reader_that_fails_says_so():
    """A reader that dies on bad input is not the adapter hanging up: the waiter's
    error names the failure (investigation 11)."""

    class Broken:
        def __iter__(self):
            raise UnicodeDecodeError("charmap", b"\x9d", 0, 1, "character maps to <undefined>")

    agent = FakeAgent()
    conn = AcpConnection(write_to=agent.to_agent_w, read_from=Broken())
    pending = conn.request("session/prompt", {})
    conn.start()
    with pytest.raises(Exception) as err:
        pending.result(timeout=5)
    assert "the reader failed: UnicodeDecodeError" in str(err.value)


def _windows_locale(monkeypatch):
    """Make every text open() that names no encoding use cp1252, as on Windows."""
    import builtins
    import pathlib

    real_open, real_read, real_write = builtins.open, pathlib.Path.read_text, pathlib.Path.write_text

    def open_(file, mode="r", *a, **kw):
        if "b" not in mode and kw.get("encoding") is None and len(a) < 3:
            kw["encoding"] = "cp1252"
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", open_)
    monkeypatch.setattr(pathlib.Path, "read_text",
                        lambda self, encoding=None, errors=None, **kw:
                        real_read(self, encoding=encoding or "cp1252", errors=errors, **kw))
    monkeypatch.setattr(pathlib.Path, "write_text",
                        lambda self, data, encoding=None, errors=None, newline=None, **kw:
                        real_write(self, data, encoding=encoding or "cp1252", errors=errors,
                                   newline=newline, **kw))


def test_the_agents_own_file_reads_and_writes_are_utf8_on_windows(tmp_path, monkeypatch):
    """The adapter sends the agent's Read/Write through these handlers. Under the
    Windows default (cp1252) a curly quote garbled on read and an arrow or emoji
    refused to write; writes also turned \\n into \\r\\n."""
    import io

    from canopy_acp.client import AcpConnection

    _windows_locale(monkeypatch)
    agent = AcpConnection(write_to=io.StringIO(), read_from=io.StringIO(), fs_root=tmp_path)
    text = "a “quoted” note → done ✓ 🎉\nsecond line\n"
    agent._write_file({"path": str(tmp_path / "notes.md"), "content": text})
    assert (tmp_path / "notes.md").read_bytes() == text.encode("utf-8")
    assert agent._read_file({"path": str(tmp_path / "notes.md")}) == text


def test_the_session_root_is_a_path_not_a_string_prefix(tmp_path):
    import io

    from canopy_acp.client import AcpConnection

    root = tmp_path / "wt"
    root.mkdir()
    (tmp_path / "wt2").mkdir()
    agent = AcpConnection(write_to=io.StringIO(), read_from=io.StringIO(), fs_root=root)
    with pytest.raises(PermissionError):
        agent._resolve_path({"path": str(tmp_path / "wt2" / "x.txt")})
    assert agent._resolve_path({"path": str(root / "x.txt")}) == (root / "x.txt").resolve()
