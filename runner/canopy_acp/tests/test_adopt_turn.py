"""A turn another process started, finished here (canopy_acp.turn.adopt_turn).

The cloud runner's daemon can be replaced mid-turn (an update): the adapter keeps
running under a relay that logged everything it said, and the new daemon replays
that log through `AcpAgent.attach`. These hold the replay rules: what came before
the prompt is history, what was already emitted is not emitted twice, an agent
request already answered is not answered again, and one still waiting is.
"""
from __future__ import annotations

import json
import threading

from canopy_acp import AcpAgent, UpdateReducer, adopt_turn


def _update(text):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {
        "sessionId": "s1",
        "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}}


def _tool(call_id, status):
    kind = "tool_call" if status == "pending" else "tool_call_update"
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {
        "sessionUpdate": kind, "toolCallId": call_id, "status": status, "title": "Bash",
        "rawInput": {"command": "ls"}}}}


class _Replay:
    """The relay's stdout, replayed: each line with the offset it sat at."""

    def __init__(self, lines):
        self._lines = lines
        self.last_offset = 0

    def __iter__(self):
        for offset, line in self._lines:
            self.last_offset = offset
            yield json.dumps(line) + "\n"


class _Stdin:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    def write(self, text):
        self.sent.append(json.loads(text))

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self, lines):
        self.stdout = _Replay(lines)
        self.stdin = _Stdin()
        self.stderr = None
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.returncode = 0


def test_an_adopted_turn_replays_only_its_own_and_emits_nothing_twice():
    lines = [
        (10, _update("an earlier turn's words, replayed by session/load")),
        # 20: the `session/prompt` (id 7) the old daemon sent — in the relay's log.
        (30, _update("Looking ")),
        (40, _tool("t1", "pending")),
        (45, {"jsonrpc": "2.0", "id": 3, "method": "session/request_permission",
              "params": {"options": [{"optionId": "allow", "kind": "allow_once"}]}}),
        (50, _tool("t1", "completed")),
        (60, _update("done.")),
        (65, {"jsonrpc": "2.0", "id": 4, "method": "session/request_permission",
              "params": {"options": [{"optionId": "allow", "kind": "allow_once"}]}}),
        (70, {"jsonrpc": "2.0", "id": 7, "result": {"stopReason": "end_turn"}}),
    ]
    proc = _Proc(lines)
    emitted: list = []
    flushed: list = []

    def attach(on_update):
        return AcpAgent.attach(proc, cwd=".", session_id="s1", prompt_id=7, next_id=8,
                               answered={3}, on_update=on_update)

    res = adopt_turn(attach=attach, reducer=UpdateReducer(), emit=emitted.extend,
                     session_id="s1", live_from=20, emitted_through=40,
                     position=lambda: proc.stdout.last_offset, on_flushed=flushed.append,
                     flush_seconds=0.05, release_wait=0)
    assert res.ok and res.session_id == "s1"
    # The whole turn's text, rebuilt; the earlier turn's replay is not in it.
    assert res.text == "Looking done."
    # tool_start went before the restart; the end and the rest are this daemon's to send.
    kinds = [e["kind"] for e in emitted]
    assert "tool_start" not in kinds and kinds.count("tool_end") == 1
    assert [e["payload"]["text"] for e in emitted if e["kind"] == "assistant"] == ["done."]
    # Request 3 was answered before the restart; 4 was waiting and is answered now.
    replies = [m for m in proc.stdin.sent if "result" in m]
    assert [m["id"] for m in replies] == [4]
    assert flushed and flushed[-1] >= 50
    # Closed at the end, as drive_turn closes its adapter.
    assert proc.stdin.closed


def test_a_connection_continues_the_request_ids_it_took_over():
    proc = _Proc([])
    gate = threading.Event()

    def quiet():  # an adapter with nothing more to say, yet
        gate.wait(5)
        return
        yield

    proc.stdout = quiet()
    agent, _pending = AcpAgent.attach(proc, cwd=".", session_id="s1", prompt_id=7, next_id=8,
                                      on_update=None)
    agent.cancel()
    agent.set_mode("bypassPermissions")
    gate.set()
    sent = proc.stdin.sent
    assert sent[0]["method"] == "session/cancel"
    assert sent[1]["id"] == 8  # never an id the old daemon used
