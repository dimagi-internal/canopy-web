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


class _Stream:
    """A turn's `session/update`s turned into ledger events: fragments joined,
    tool starts held for their arguments, flushed in batches.

    Position hooks, for a turn whose adapter outlives its runner (an adopted turn):
    `position()` names the line being dispatched (any comparable marker); an update
    at or before `live_from` is history and is ignored; one at or before
    `emitted_through` is applied (it is part of this turn's text) but not emitted
    again; `on_flushed(marker)` is told the last update a successful emit carried."""

    def __init__(self, reducer, emit, *, log, clock, position=None, on_flushed=None,
                 live_from=None, emitted_through=None):
        self.reducer = reducer
        self._emit = emit
        self._log = log
        self._clock = clock
        self._position = position
        self._on_flushed = on_flushed
        self._live_from = live_from
        self._emitted_through = emitted_through
        self.lock = threading.Lock()
        self.batch: list = []
        self.message: list = []
        self.started: set = set()
        self.complete: set = set()
        self.replaying = False
        self.at = clock()
        self._last_pos = None

    def _close_message(self, quiet: bool = False):
        text = "".join(self.message)
        self.message.clear()
        if text.strip() and not quiet:
            self.batch.append({"kind": "assistant", "payload": {"text": text}})

    def on_update(self, _session_id, update):
        if self.replaying:
            return
        pos = self._position() if self._position is not None else None
        if pos is not None and self._live_from is not None and pos <= self._live_from:
            return
        quiet = (pos is not None and self._emitted_through is not None
                 and pos <= self._emitted_through)
        self.at = self._clock()
        kind = self.reducer.apply(update)
        if kind is None:
            return
        with self.lock:
            if kind == "agent_message_chunk":
                text = content_text(update)
                if text:
                    self.message.append(text)
            elif kind in ("tool_call", "tool_call_update"):
                # A message that ended before the restart was emitted then.
                self._close_message(quiet=quiet)
                call_id = update.get("toolCallId") or ""
                call = self.reducer.tool_call(call_id)
                if call is None:
                    return
                if call_id not in self.started and (call.raw_input or call.is_complete):
                    self.started.add(call_id)
                    if not quiet:
                        self.batch.append({"kind": "tool_start", "payload": {
                            "id": call_id, "name": call.tool_name or call.kind,
                            "input": call.raw_input}})
                if call.is_complete and call_id not in self.complete:
                    self.complete.add(call_id)
                    if not quiet:
                        self.batch.append({"kind": "tool_end", "payload": {
                            "tool_use_id": call_id, "is_error": call.is_error,
                            "content": call.result_text}})
            if pos is not None:
                self._last_pos = pos

    def flush(self, final=False):
        with self.lock:
            if final:
                self._close_message()
            marker = self._last_pos
            if not self.batch:
                return
            pending, self.batch[:] = list(self.batch), []
        try:
            self._emit(pending)
        except Exception as exc:  # noqa: BLE001 — the live stream may never cost a turn
            self._log(f"warn: could not emit ACP events: {exc}")
            return
        if self._on_flushed is not None and marker is not None:
            try:
                self._on_flushed(marker)
            except Exception:  # noqa: BLE001
                pass


def _await_reply(agent, pending, stream: _Stream, *, cancelled, was_cancelled: bool,
                 flush_seconds: float, idle_timeout, timeout, clock):
    """Wait for the prompt's reply, flushing and honouring a stop meanwhile.
    Returns (result, was_cancelled)."""
    deadline = None if timeout is None else clock() + timeout
    stream.at = clock()
    while True:
        try:
            result = pending.result(timeout=flush_seconds)
            return result, was_cancelled
        except TimeoutError:
            stream.flush()
            if not was_cancelled and cancelled():
                was_cancelled = True
                agent.cancel()
            now = clock()
            if idle_timeout is not None and now - stream.at > idle_timeout:
                agent.cancel()
                raise TurnError(f"ACP turn silent for {idle_timeout}s") from None
            if deadline is not None and now > deadline:
                agent.cancel()
                raise TurnError(f"ACP turn exceeded {timeout}s") from None


def _result_of(agent, result, stream: _Stream, session_id: str, was_cancelled: bool, init,
               log) -> TurnResult:
    stream.flush(final=True)
    reducer = stream.reducer
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


def _turn_error(exc, agent, stream: _Stream, session_id: str) -> TurnError:
    stream.flush(final=True)
    detail = exc.detail if isinstance(exc, TurnError) else str(exc)
    why = ""
    try:
        why = agent.exit_report() if agent is not None else ""
    except Exception:  # noqa: BLE001 — older or fake agents have none
        pass
    return TurnError(f"{detail} ({why})" if why else detail,
                     session_id or getattr(agent, "session_id", "") or "")


def _release(agent, on_end, session_id, release_wait, processes):
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


def drive_turn(prompt: str, *, make_agent, reducer, emit, cwd, env: dict | None = None,
               resume: str | None = None, on_start=None, on_session=None, on_end=None,
               cancelled=lambda: False, session_start_timeout: float = 300,
               flush_seconds: float = 10, idle_timeout: float | None = None,
               timeout: float | None = None, clock=time.time, log=logger.info,
               release_wait: float = RELEASE_WAIT_SECONDS,
               processes=session_processes, mode: str | None = None,
               position=None, on_flushed=None) -> TurnResult:
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
    fresh session — `on_session` then names the new id. `position`/`on_flushed`:
    see `_Stream` — how a runner records what it has emitted, so a turn it
    adopts after a restart (`adopt_turn`) emits nothing twice.

    Raises TurnError when the turn could not run to an end."""
    stream = _Stream(reducer, emit, log=log, clock=clock, position=position,
                     on_flushed=on_flushed)
    agent = None
    session_id = ""
    try:
        agent = make_agent(cwd=cwd, env=env, on_update=stream.on_update)
        init = agent.start()
        if on_start is not None:
            on_start(agent)
        if resume:
            stream.replaying = True
            try:
                agent.load_session(resume, timeout=session_start_timeout)
                session_id = resume
            except Exception as exc:  # noqa: BLE001
                log(f"session/load of {resume[:8]} failed ({exc}); starting fresh")
            finally:
                stream.replaying = False
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
        result, was_cancelled = _await_reply(
            agent, pending, stream, cancelled=cancelled, was_cancelled=was_cancelled,
            flush_seconds=flush_seconds, idle_timeout=idle_timeout, timeout=timeout, clock=clock)
        return _result_of(agent, result, stream, session_id, was_cancelled, init, log)
    except Exception as exc:  # noqa: BLE001 — named, with how the adapter went
        raise _turn_error(exc, agent, stream, session_id) from exc
    finally:
        _release(agent, on_end, session_id, release_wait, processes)


def adopt_turn(*, attach, reducer, emit, session_id: str, live_from, emitted_through=None,
               position=None, on_flushed=None, on_start=None, on_end=None,
               cancelled=lambda: False, flush_seconds: float = 10, clock=time.time,
               log=logger.info, release_wait: float = RELEASE_WAIT_SECONDS,
               processes=session_processes) -> TurnResult:
    """Finish a turn another process started: its adapter is still running (or has
    just ended) and its whole output is replayed from the start.

    `attach(on_update)` returns `(agent, pending)` — `AcpAgent.attach` over the
    replayed stream, and the waiter for the `session/prompt` in flight. Updates
    at or before `live_from` (the prompt) are an earlier turn's or a `session/load`
    replay and are ignored; ones at or before `emitted_through` rebuild this turn's
    text without being emitted twice. Same result and errors as `drive_turn`."""
    stream = _Stream(reducer, emit, log=log, clock=clock, position=position,
                     on_flushed=on_flushed, live_from=live_from,
                     emitted_through=emitted_through)
    agent = None
    try:
        agent, pending = attach(stream.on_update)
        if on_start is not None:
            on_start(agent)
        result, was_cancelled = _await_reply(
            agent, pending, stream, cancelled=cancelled, was_cancelled=False,
            flush_seconds=flush_seconds, idle_timeout=None, timeout=None, clock=clock)
        return _result_of(agent, result, stream, session_id, was_cancelled, None, log)
    except Exception as exc:  # noqa: BLE001
        raise _turn_error(exc, agent, stream, session_id) from exc
    finally:
        _release(agent, on_end, session_id, release_wait, processes)
