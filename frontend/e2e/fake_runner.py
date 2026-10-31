"""A scripted runner for the browser suite: everything a real runner does except the model.

It speaks the runner protocol through the real runner client (`canopy_runner.client`),
writes a real Claude Code `.jsonl` transcript for each session, and ships its rows
with `canopy_transcript` — the library both production runners use. So the server's
ingest, the socket, the reducer and the rendering under test are all the real thing;
only the agent's words are scripted.

What the agent does is chosen by the message the browser sends:

    hello                 one tool call, then "echo: hello"
    e2e:tools N           N tool calls, then a reply (a tool-heavy turn)
    e2e:slow N            N tool calls one second apart (watch a turn live)
    e2e:hang              one tool call, then wait until a person stops it
    e2e:fail              one tool call, then the turn fails
    e2e:newfile           the conversation continues in a NEW transcript file,
                          as after a failover or /clear (a new ordinal epoch)

Run by `e2e/backend.sh` beside the API; reads the runner id and token the seed wrote.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "runner" / "canopy_runner"))
sys.path.insert(0, str(REPO / "packages" / "canopy_transcript"))

from canopy_runner.client import Client  # noqa: E402
from canopy_transcript import conversational_messages, stream_event  # noqa: E402

AUTH = HERE / ".auth"
BASE = os.environ.get("E2E_API_BASE", "http://127.0.0.1:8000")
TICK = 0.3


def log(*args) -> None:
    print("[fake-runner]", *args, flush=True)


class Conversation:
    """One session's transcript on disk, and how much of it the server holds."""

    def __init__(self, root: Path, session_id: str):
        self.session_id = session_id
        self.root = root
        self.records: list[dict] = []
        self.shipped = -1
        self.transcript_id = str(uuid.uuid4())

    def new_file(self) -> None:
        """Continue in a fresh transcript file: new id, ordinals restart at 0."""
        self.records, self.shipped, self.transcript_id = [], -1, str(uuid.uuid4())

    @property
    def path(self) -> Path:
        return self.root / f"{self.transcript_id}.jsonl"

    def append(self, record: dict) -> None:
        record = {"uuid": str(uuid.uuid4()), "sessionId": self.transcript_id,
                  "cwd": str(self.root), "timestamp": _now(), **record}
        self.records.append(record)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def unshipped(self) -> list[dict]:
        rows = conversational_messages(self.records, self.shipped)
        if rows:
            self.shipped = rows[-1]["index"]
        return rows


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def say(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "id": f"msg_{uuid.uuid4().hex[:12]}",
                                             "content": [{"type": "text", "text": text}]}}


def tool_call(n: int) -> tuple[dict, dict]:
    tid = f"toolu_{uuid.uuid4().hex[:16]}"
    use = {"type": "assistant", "message": {"role": "assistant", "id": f"msg_{uuid.uuid4().hex[:12]}",
                                            "content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                                         "input": {"command": f"echo step {n}"}}]}}
    result = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": f"step {n}"}]}}
    return use, result


class FakeRunner:
    def __init__(self) -> None:
        self.runner_id = (AUTH / "runner-id.txt").read_text().strip()
        self.client = Client(BASE, (AUTH / "runner-token.txt").read_text().strip())
        self.home = Path(os.environ.get("E2E_RUNNER_HOME", "/tmp/canopy-e2e-runner")) / self.runner_id
        self.home.mkdir(parents=True, exist_ok=True)
        self.conversations: dict[str, Conversation] = {}
        self.active: set[str] = set()
        self.stopped: set[str] = set()
        self.turn_of_session: dict[str, str] = {}

    # --- the protocol -----------------------------------------------------

    def ship(self, convo: Conversation) -> None:
        rows = convo.unshipped()
        if rows:
            self.client.post_session_stream(self.runner_id, convo.session_id,
                                             [stream_event(r) for r in rows],
                                             transcript_id=convo.transcript_id)

    def step(self, convo: Conversation, *records: dict, pause: float = 0.05) -> None:
        for r in records:
            convo.append(r)
        self.ship(convo)
        time.sleep(pause)

    def run_turn(self, turn: dict) -> None:
        tid = turn["id"]
        ref = turn.get("origin_ref") or {}
        session_id = ref.get("chat_session_id") or ""
        if not session_id:
            self.client.finish(tid, note="fake runner: not a chat turn", status="failed")
            return
        thread_key = ref.get("thread_key") or session_id
        self.turn_of_session[session_id] = tid
        prompt = (turn.get("prompt") or "").strip().splitlines()[-1:] or [""]
        text = prompt[0]
        convo = self.conversations.get(session_id)
        if convo is None:
            convo = self.conversations[session_id] = Conversation(self.home / session_id, session_id)
            convo.root.mkdir(parents=True, exist_ok=True)
        elif text.startswith("e2e:newfile"):
            convo.new_file()
        self.client.record_session(self.runner_id, turn.get("agent_slug") or "", thread_key,
                                   project=turn.get("project") or "", workspace=turn.get("workspace") or "",
                                   emdash_task_id=convo.transcript_id, session_id=convo.transcript_id)
        self.client.start(tid, session_id=convo.transcript_id)
        status, note = "done", "ok"
        try:
            status, note = self.script(tid, convo, text)
        except Exception as exc:  # noqa: BLE001 — a scripted failure must still end the turn
            traceback.print_exc()
            status, note = "failed", f"fake runner error: {exc}"
        if status:
            self.client.finish(tid, note=note, status=status, emdash_task_id=convo.transcript_id)

    def script(self, tid: str, convo: Conversation, text: str) -> tuple[str, str]:
        m = re.match(r"e2e:(\w+)\s*(\d*)", text)
        verb, n = (m.group(1), int(m.group(2) or 0)) if m else ("echo", 0)
        self.step(convo, user(text))
        if verb in ("tools", "slow"):
            for i in range(n or 1):
                if tid in self.stopped:
                    return "cancelled", "stopped"
                self.step(convo, *tool_call(i + 1), pause=1.0 if verb == "slow" else 0.05)
            self.step(convo, say(f"done after {n} tool calls"))
        elif verb == "hang":
            self.step(convo, *tool_call(1))
            while tid not in self.stopped:
                time.sleep(TICK)
            return "cancelled", "stopped"
        elif verb == "fail":
            self.step(convo, *tool_call(1))
            return "failed", "scripted failure"
        else:
            self.step(convo, *tool_call(1))
            self.step(convo, say(f"echo: {text}"))
        return "done", "ok"

    # --- the loop ---------------------------------------------------------

    def tick(self) -> None:
        reply = self.client.heartbeat(self.runner_id, sorted(self.active), host="e2e-fake-runner")
        # A stopped turn comes back on the heartbeat, as it does for the real runners.
        self.stopped.update((reply or {}).get("cancel_turn_ids") or [])
        turn = self.client.claim(self.runner_id)
        if turn and turn.get("id"):
            log("claimed", turn["id"], (turn.get("prompt") or "")[-60:])
            self.active.add(turn["id"])
            threading.Thread(target=self._run, args=(turn,), daemon=True).start()
        # A person's Stop: the durable record, drained here as the real runners do.
        for stop in self.client.sync_session_interrupts(self.runner_id):
            tid = self.turn_of_session.get(stop["session_id"])
            if tid:
                self.stopped.add(tid)
            self.client.post_session_interrupt_result(self.runner_id, stop["session_id"],
                                                      stop["interrupt_id"], "interrupted")

    def _run(self, turn: dict) -> None:
        try:
            self.run_turn(turn)
        finally:
            self.active.discard(turn["id"])

    def forever(self) -> None:
        log("up as", self.runner_id, "against", BASE)
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a bad tick must not stop the runner
                traceback.print_exc()
            time.sleep(TICK)


if __name__ == "__main__":
    FakeRunner().forever()
