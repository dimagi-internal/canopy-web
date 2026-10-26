"""Driving one turn over ACP — the loop both runners run.

The cloud runner (`run_acp` in runner/ec2/cloud_runner.py) and the laptop
runner's `claude-cli` runtime (runner/canopy_runner/canopy_runner/headless.py) each
wrap this with their own bookkeeping (stop registries, leases, session indexes)
and nothing else. Everything about turning a stream of `session/update`s into
the ledger's events, and about when a turn is over, lives here once.

What it gets right, each found on a live box:

* **Replay is not news.** `session/load` replays the whole prior conversation as
  ordinary updates; emitted, every resumed turn would append the history to the
  ledger again and fold it into this turn's result text.
* **A reply is emitted whole.** ACP sends a reply as many `agent_message_chunk`
  fragments, often a few characters each, and every assistant event is one row
  downstream — one Slack post. Fragments are joined and the message emitted when
  the agent moves on to a tool call or the turn ends (an Eva reply once posted
  as 85 messages split mid-word, 2026-10-06).
* **A tool call is emitted with its arguments.** `tool_call` opens with
  `rawInput: {}`; the start waits for the first update that has input, or for an
  argument-less call to finish. `tool_end` is emitted once, at a terminal status
  (`tool_call_update` is a sparse patch; see updates.py).
* **No time limit by default**, like `claude -p`. A wall-clock deadline killed a
  busy 90-minute ACE run, and an idle limit would kill a long quiet tool call the
  same way (2026-10-18). A stuck turn is ended by a stop; `idle_timeout` and
  `timeout` exist for a caller that wants a cap.
* **The session is free when the turn returns.** The Agent SDK's own `claude`
  process outlives the adapter by a few seconds (measured 2-4s, adapter 0.93.0),
  and two processes on one session fork it silently, so the driver waits for it.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("canopy_acp.turn")

#: How long a finished turn waits for the session's last process to exit.
RELEASE_WAIT_SECONDS = 15.0


@dataclass
class TurnResult:
    ok: bool
    text: str
    session_id: str
    stop_reason: str = ""
    cancelled: bool = False
    #: The adapter's last rate-limit meta ({} when none arrived). A `rejected`
    #: status fails the turn even when the adapter ended it with `end_turn`.
    rate_limit: dict = field(default_factory=dict)


class TurnError(RuntimeError):
    """The turn could not run to an end. `detail` says why, including how the
    adapter went if it did; `session_id` is the session it reached, if any."""

    def __init__(self, detail: str, session_id: str = ""):
        super().__init__(detail)
        self.detail = detail
        self.session_id = session_id


def content_text(update: dict) -> str:
    """The text of an `agent_message_chunk`, tolerant of both block shapes."""
    content = update.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def session_processes(session_id: str) -> list[int]:
    """PIDs of this user's processes whose command line names the session.
    Windows has no pgrep (or os.getuid): a CIM query reads command lines there."""
    if sys.platform == "win32":
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                # Not itself: this query's own command line names the session too.
                "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
                f"$_.CommandLine -like '*{session_id}*' }} | ForEach-Object {{ $_.ProcessId }}"]
    else:
        argv = ["pgrep", "-U", str(os.getuid()), "-f", session_id]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in out if p.isdigit() and int(p) != os.getpid()]


def wait_released(session_id: str, wait: float = RELEASE_WAIT_SECONDS,
                  processes=session_processes) -> bool:
    """Wait until no process names the session. True once none does."""
    deadline = time.monotonic() + wait
    while processes(session_id):
        if time.monotonic() > deadline:
            logger.warning("a process still names session %s after %.0fs", session_id, wait)
            return False
        time.sleep(0.5)
    return True


def stop_origin(stop: str, canopy_cancelled: bool, init) -> str:
    """Who ended a turn that did not end cleanly, and which adapter build did it.

    On 2026-10-26 turn 068076b8 (canopy-web on demo-0fc9c7b1efb87c5c3, failure
    investigation 14) failed "turn ended: cancelled" with no stop requested, and
    the note could not say whether canopy had sent the cancel or the adapter
    ended the turn on its own, nor which claude-agent-acp the box runs (cloud
    boxes install it unpinned). Both decide where the fix goes."""
    info = (init or {}).get("agentInfo") if isinstance(init, dict) else None
    version = info.get("version") if isinstance(info, dict) else ""
    adapter = f"claude-agent-acp {version}" if version else "adapter version unknown"
    if stop == "cancelled":
        who = "canopy sent the cancel" if canopy_cancelled else "the adapter's own: canopy sent no cancel"
        return f"{who}; {adapter}"
    return adapter


def drive_turn(prompt: str, *, make_agent, reducer, emit, cwd, env: dict | None = None,
               resume: str | None = None, on_start=None, on_session=None, on_end=None,
               cancelled=lambda: False, session_start_timeout: float = 300,
               flush_seconds: float = 10, idle_timeout: float | None = None,
               timeout: float | None = None, clock=time.time, log=logger.info,
               release_wait: float = RELEASE_WAIT_SECONDS,
               processes=session_processes, mode: str | None = None) -> TurnResult:
    """Run one prompt as one turn and close the adapter.

    `make_agent(cwd=, env=, on_update=)` builds the agent (canopy_acp.AcpAgent,
    or a test's); `reducer` is an UpdateReducer. Hooks, all optional:
    `on_start(agent)` once it is up (register it for a stop from another thread),
    `on_session(sid)` once the session exists and before the prompt goes in,
    `on_end(agent)` before it is closed (unregister it: a cancel arriving after
    the close would reach a dead connection). `mode` is the permission mode the
    session is put in before the prompt (`session/set_mode`, every turn: each is a
    fresh adapter, which starts in `default`); a refusal is logged and the turn runs
    in the mode it has. `cancelled()` is polled; true sends
    the Escape equivalent once. A resume whose `session/load` fails starts a
    fresh session — `on_session` then names the new id.

    Raises TurnError when the turn could not run to an end."""
    lock = threading.Lock()
    batch: list = []
    message: list = []
    started: set = set()
    complete: set = set()
    state = {"replaying": False, "at": clock()}

    def close_message():
        text = "".join(message)
        message.clear()
        if text.strip():
            batch.append({"kind": "assistant", "payload": {"text": text}})

    def on_update(_session_id, update):
        if state["replaying"]:
            return
        state["at"] = clock()
        kind = reducer.apply(update)
        if kind is None:
            return
        with lock:
            if kind == "agent_message_chunk":
                text = content_text(update)
                if text:
                    message.append(text)
            elif kind in ("tool_call", "tool_call_update"):
                close_message()
                call_id = update.get("toolCallId") or ""
                call = reducer.tool_call(call_id)
                if call is None:
                    return
                if call_id not in started and (call.raw_input or call.is_complete):
                    started.add(call_id)
                    batch.append({"kind": "tool_start", "payload": {
                        "id": call_id, "name": call.tool_name or call.kind, "input": call.raw_input}})
                if call.is_complete and call_id not in complete:
                    complete.add(call_id)
                    batch.append({"kind": "tool_end", "payload": {
                        "tool_use_id": call_id, "is_error": call.is_error,
                        "content": call.result_text}})

    def flush(final=False):
        with lock:
            if final:
                close_message()
            if not batch:
                return
            pending, batch[:] = list(batch), []
        try:
            emit(pending)
        except Exception as exc:  # noqa: BLE001 — the live stream may never cost a turn
            log(f"warn: could not emit ACP events: {exc}")

    agent = None
    session_id = ""
    try:
        agent = make_agent(cwd=cwd, env=env, on_update=on_update)
        init = agent.start()
        if on_start is not None:
            on_start(agent)
        if resume:
            state["replaying"] = True
            try:
                agent.load_session(resume, timeout=session_start_timeout)
                session_id = resume
            except Exception as exc:  # noqa: BLE001
                log(f"session/load of {resume[:8]} failed ({exc}); starting fresh")
            finally:
                state["replaying"] = False
                # A replay update that lands just after the load reply would
                # otherwise seed this turn's reply with the last one's.
                reducer.reset_stream_state()
        if not session_id:
            session_id = agent.new_session(timeout=session_start_timeout)
        if mode:
            try:
                agent.set_mode(mode).result(timeout=session_start_timeout)
            except Exception as exc:  # noqa: BLE001 — the turn still runs, in the adapter's mode
                log(f"warn: could not put session {session_id[:8]} in {mode} mode: {exc}")
        if on_session is not None:
            on_session(session_id)

        pending = agent.prompt(prompt)
        was_cancelled = False
        if cancelled():
            # Arrived while the session was starting, when there was no prompt yet.
            was_cancelled = True
            agent.cancel()
        deadline = None if timeout is None else clock() + timeout
        state["at"] = clock()
        while True:
            try:
                result = pending.result(timeout=flush_seconds)
                break
            except TimeoutError:
                flush()
                if not was_cancelled and cancelled():
                    was_cancelled = True
                    agent.cancel()
                now = clock()
                if idle_timeout is not None and now - state["at"] > idle_timeout:
                    agent.cancel()
                    raise TurnError(f"ACP turn silent for {idle_timeout}s") from None
                if deadline is not None and now > deadline:
                    agent.cancel()
                    raise TurnError(f"ACP turn exceeded {timeout}s") from None
        flush(final=True)

        stop = (result or {}).get("stopReason", "")
        rate_limit = dict(reducer.rate_limit or {})
        ok = stop in ("end_turn", "max_tokens") and rate_limit.get("status") != "rejected"
        text = reducer.assistant_text.strip()
        if stop not in ("end_turn", "max_tokens"):
            # A turn failed on its stop reason must say which one. On 2026-10-26 turn
            # cfab21f5 (demo runner, failure investigation 12) failed with only the
            # agent's last words — "Standalone sleep is blocked, so I'll run it in the
            # background." — and nothing recorded why it was graded failed.
            reason = f"turn ended: {stop or 'unknown'} ({stop_origin(stop, was_cancelled, init)})"
            text = f"{text}\n\n{reason}" if text else reason
            tail = getattr(agent, "stderr_tail", lambda: "")()
            if tail:
                log(f"ACP turn ended {stop or 'unknown'}; adapter stderr: {tail}")
        return TurnResult(ok=ok, text=text, session_id=session_id, stop_reason=stop,
                          cancelled=was_cancelled or stop == "cancelled", rate_limit=rate_limit)
    except Exception as exc:  # noqa: BLE001 — named, with how the adapter went
        flush(final=True)
        detail = exc.detail if isinstance(exc, TurnError) else str(exc)
        why = ""
        try:
            why = agent.exit_report() if agent is not None else ""
        except Exception:  # noqa: BLE001 — older or fake agents have none
            pass
        raise TurnError(f"{detail} ({why})" if why else detail,
                         session_id or getattr(agent, "session_id", "") or "") from exc
    finally:
        if on_end is not None and agent is not None:
            try:
                on_end(agent)
            except Exception:  # noqa: BLE001
                pass
        if agent is not None:
            try:
                agent.close()
            except Exception:  # noqa: BLE001
                pass
        if session_id and release_wait > 0:
            wait_released(session_id, release_wait, processes)
