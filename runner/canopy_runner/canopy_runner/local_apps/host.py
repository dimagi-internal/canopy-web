"""The detached supervisor of one local app run.

`canopy-runner app start` launches this as its own process, and it launches the source
(if any) and the app. It is the only part that holds a credential, and the only part
that can stop an app regardless of what the app does, so nothing an app does may block
it: every child pipe has its own thread, and every server call is off the main loop.

Log rule. Stdout logs end up in agent transcripts, and a meeting's words must never get
there, so this is enforced here and not asked of apps:
  * stream items are relayed and never written anywhere;
  * event and status fields are numbers or short identifiers, and anything else is
    replaced by its length (`session_url`, the one thing the person needs, is exempt);
  * stderr from a sensitive run is dropped unread;
  * heartbeats carry a short state and numeric counts only.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import server as server_mod
from . import state
from .manifest import PYTHON

IDENTIFIER_RX = re.compile(r"^[A-Za-z0-9_.:\-]{1,40}$")
URL_RX = re.compile(r"^https?://[^\s\"'<>]{1,500}$")
URL_FIELD = "session_url"
STREAM_TYPES = {"segment", "ended", "gone"}
STDERR_KEEP = 500
LOG_ROTATE_BYTES = 1024 * 1024

STOPPED_BY_YOU = "stopped_by_you"
MAX_MINUTES = "max_minutes"
UNREACHABLE = "server_unreachable"
REMOVED = "removed"
SOURCE_GONE = "source_gone"
SOURCE_ENDED = "source_ended"
SOURCE_EXITED = "source_exited"
APP_EXITED = "app_exited"
APP_CRASHED = "app_crashed"
APP_NOT_READING = "app_not_reading"
GONE_RESULT_STATUS = 410


@dataclasses.dataclass
class Limits:
    kill_after: float = 5.0
    heartbeat_every: float = 15.0
    unreachable_after: float = 300.0
    # How long an app has to send its last words once its source says the meeting ended.
    ended_grace: float = 120.0
    tick: float = 0.2
    max_line: int = 1024 * 1024
    send_queue: int = 5000
    pending_calls: int = 16
    exit_drain: float = 1.0


@dataclasses.dataclass
class ChildSpec:
    key: str
    version: str
    entry: list
    cwd: str
    settings: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class RunSpec:
    key: str
    version: str
    run_id: str
    max_minutes: float
    canopy: list
    sensitive: bool
    unpublished: bool
    app: ChildSpec
    source: Optional[ChildSpec] = None
    config: str = ""

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, raw: dict) -> "RunSpec":
        raw = dict(raw)
        raw["app"] = ChildSpec(**raw["app"])
        raw["source"] = ChildSpec(**raw["source"]) if raw.get("source") else None
        return cls(**raw)


def clean_identifier(value, default: str) -> str:
    return value if isinstance(value, str) and IDENTIFIER_RX.match(value) else default


def clean_fields(fields) -> dict:
    """The log rule for event fields."""
    if not isinstance(fields, dict):
        return {}
    out = {}
    for name, value in fields.items():
        if not isinstance(name, str) or not IDENTIFIER_RX.match(name):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            out[name] = value
        elif isinstance(value, str):
            if IDENTIFIER_RX.match(value) or (name == URL_FIELD and URL_RX.match(value)):
                out[name] = value
            else:
                out[name] = len(value)
        else:
            try:
                out[name] = len(json.dumps(value))
            except (TypeError, ValueError):
                out[name] = 0
    return out


def clean_counts(counts) -> dict:
    """Numbers under identifier names, and nothing else, for heartbeats and status."""
    if not isinstance(counts, dict):
        return {}
    return {name: value for name, value in counts.items()
            if isinstance(name, str) and IDENTIFIER_RX.match(name)
            and isinstance(value, (int, float)) and not isinstance(value, bool)}


class Child:
    """One running program and the threads that keep its pipes from blocking the host."""

    def __init__(self, role: str, spec: ChildSpec, inbox: queue.Queue, limits: Limits, on_stderr):
        self.role, self.spec, self.inbox, self.limits = role, spec, inbox, limits
        self.on_stderr = on_stderr
        self.proc: Optional[subprocess.Popen] = None
        self.out_queue: queue.Queue = queue.Queue(maxsize=limits.send_queue)
        self.eof = False
        self.eof_at: Optional[float] = None
        self.exited_at: Optional[float] = None
        self.exit_handled = False
        self.killed = False

    def start(self) -> None:
        argv = [sys.executable if i == 0 and part == PYTHON else part
                for i, part in enumerate(self.spec.entry)]
        # Its own session, so a kill reaches whatever it started too.
        self.proc = subprocess.Popen(argv, cwd=self.spec.cwd, env=state.scrubbed_env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
        for target in (self._read_stdout, self._read_stderr, self._write_stdin):
            threading.Thread(target=target, daemon=True).start()

    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        limit = self.limits.max_line
        try:
            while True:
                raw = stream.readline(limit + 1)
                if not raw:
                    break
                if len(raw) > limit and not raw.endswith(b"\n"):
                    # Too long to be a protocol line: skip to its end, and let it count as bad.
                    while raw and not raw.endswith(b"\n"):
                        raw = stream.readline(limit + 1)
                    self.inbox.put(("line", self.role, b""))
                    continue
                self.inbox.put(("line", self.role, raw))
        except (OSError, ValueError):
            pass
        self.inbox.put(("eof", self.role, None))

    def _read_stderr(self) -> None:
        try:
            for raw in iter(lambda: self.proc.stderr.readline(self.limits.max_line), b""):
                self.on_stderr(self.role, raw)
        except (OSError, ValueError):
            pass

    def _write_stdin(self) -> None:
        stream = self.proc.stdin
        try:
            while True:
                item = self.out_queue.get()
                if item is None:
                    break
                stream.write(item)
                stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def send(self, message: dict) -> bool:
        """False when the program has stopped reading and its queue is full."""
        try:
            self.out_queue.put_nowait(json.dumps(message).encode("utf-8") + b"\n")
            return True
        except queue.Full:
            return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if not self.proc:
            return
        self.killed = True
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Host:
    def __init__(self, spec: RunSpec, link, limits: Optional[Limits] = None):
        self.spec, self.link = spec, link
        self.limits = limits or Limits()
        self.inbox: queue.Queue = queue.Queue()
        self.calls: queue.Queue = queue.Queue()
        self.pending_calls = 0
        self.app: Optional[Child] = None
        self.source: Optional[Child] = None
        self.lock = state.HostLock(spec.key)
        self.log_lock = threading.Lock()
        self.stats_lock = threading.Lock()

        self.run_state = "starting"
        self.counts: dict = {}
        self.bad_lines = 0
        self.relayed = 0
        self.record: dict = {}

        self.started = 0.0
        self.last_server_ok = 0.0
        self.stop_reason: Optional[str] = None
        self.stopping_at: Optional[float] = None
        self.external_stop: Optional[str] = None
        self.source_closed = False  # no stream item goes to the app after this
        self.source_ended_at: Optional[float] = None
        self.app_said: Optional[str] = None
        self.source_said: Optional[str] = None
        self.done = threading.Event()

    # --- records -------------------------------------------------------------------

    def _log(self, kind: str, **fields) -> None:
        with self.log_lock:
            state.append_private_line(state.log_file(self.spec.key),
                                      {"t": time.time(), "kind": kind, **fields})

    def _save(self, **changes) -> None:
        self.record.update(changes)
        self.record["counts"] = self._heartbeat_counts()
        state.write_json(state.run_file(self.spec.key), self.record)

    def _heartbeat_counts(self) -> dict:
        counts = dict(self.counts)
        if self.bad_lines:
            counts["bad_lines"] = self.bad_lines
        return counts

    def _on_stderr(self, role: str, raw: bytes) -> None:
        if self.spec.sensitive:
            return
        self._log("stderr", role=role, text=raw[:STDERR_KEEP].decode("utf-8", "replace").rstrip())

    # --- stopping ------------------------------------------------------------------

    def request_stop(self, reason: str = STOPPED_BY_YOU) -> None:
        """Safe from a signal handler or another thread: the main loop acts on it."""
        self.external_stop = reason

    def begin_stop(self, reason: str) -> None:
        if self.stop_reason is not None:
            return
        self.stop_reason = reason
        self.stopping_at = time.monotonic()
        self.source_closed = True
        self._log("stopping", reason=reason)
        for child in (self.app, self.source):
            if child and child.alive():
                child.send({"type": "stop", "reason": reason})
        self._save(state="stopping")

    def _relay_gone_then_stop(self, reason: str) -> None:
        """`gone` reaches the app before `stop`, so unsent words are dropped, not sent."""
        if self.app and self.app.alive() and not self.source_closed:
            self.app.send({"type": "gone"})
        self.begin_stop(reason)

    # --- messages ------------------------------------------------------------------

    def _note_bad(self) -> None:
        # The line itself is never logged: it may be a piece of someone's conversation.
        self.bad_lines += 1

    def _on_line(self, role: str, raw: bytes) -> None:
        try:
            message = json.loads(raw)
            kind = message["type"]
            if not isinstance(kind, str):
                raise TypeError
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            self._note_bad()
            return
        if role == "source":
            self._on_source(message, kind)
        else:
            self._on_app(message, kind)

    def _on_common(self, role: str, message: dict, kind: str) -> bool:
        if kind == "event":
            name = clean_identifier(message.get("name"), "event")
            fields = clean_fields(message.get("fields"))
            self._log("event", role=role, name=name, fields=fields)
            changes = {"last_event": name, "last_event_at": time.time()}
            if isinstance(fields.get(URL_FIELD), str) and URL_RX.match(fields[URL_FIELD]):
                changes[URL_FIELD] = fields[URL_FIELD]
            self._save(**changes)
            return True
        return False

    def _on_app(self, message: dict, kind: str) -> None:
        if self._on_common("app", message, kind):
            return
        if kind == "status":
            new_state = clean_identifier(message.get("state"), self.run_state)
            self.counts = clean_counts(message.get("counts"))
            if new_state != self.run_state:
                self._log("status", state=new_state)
            self.run_state = new_state
            if self.stop_reason is None:
                self._save(state=new_state)
        elif kind == "canopy.call":
            self._on_canopy_call(message)
        elif kind == "stopped":
            self.app_said = clean_identifier(message.get("reason"), APP_EXITED)
        else:
            self._note_bad()

    def _on_source(self, message: dict, kind: str) -> None:
        if self._on_common("source", message, kind):
            return
        if kind in STREAM_TYPES:
            self._on_stream(message, kind)
        elif kind == "stopped":
            self.source_said = clean_identifier(message.get("reason"), SOURCE_EXITED)
        elif kind == "canopy.call":
            # The run's Canopy rights are the app's; a source reads and reports.
            call_id = message.get("id")
            self.source.send({"type": "canopy.result", "id": call_id, "status": 403,
                              "body": {"detail": "a source makes no Canopy calls"}})
        elif kind != "status":
            self._note_bad()

    def _on_stream(self, message: dict, kind: str) -> None:
        if self.source_closed:
            return
        if kind == "gone":
            self._relay_gone_then_stop(SOURCE_GONE)
            return
        if not self.app.send(message):
            self.begin_stop(APP_NOT_READING)
            return
        self.relayed += 1
        if kind == "ended":
            self.source_closed = True
            self.source_ended_at = time.monotonic()
            self._log("source_ended")

    def _on_canopy_call(self, message: dict) -> None:
        call_id, op, args = message.get("id"), message.get("op"), message.get("args", {})
        if not isinstance(call_id, (str, int)) or isinstance(call_id, bool) or not isinstance(op, str):
            self._note_bad()
            return
        refusal = None
        if self.stop_reason is not None:
            refusal = (409, "this run is stopping")
        elif op not in self.spec.canopy:
            refusal = (403, "the manifest does not declare this call")
        elif not isinstance(args, dict):
            refusal = (400, "args must be an object")
        elif self.pending_calls >= self.limits.pending_calls:
            refusal = (429, "too many calls waiting")
        if refusal:
            self.app.send({"type": "canopy.result", "id": call_id, "status": refusal[0],
                           "body": {"detail": refusal[1]}})
            self._log("canopy", op=clean_identifier(op, "unknown"), status=refusal[0])
            return
        self.pending_calls += 1
        self.calls.put((call_id, op, args))

    def _on_canopy_result(self, call_id, op: str, status: int, body: dict) -> None:
        self.pending_calls -= 1
        self._log("canopy", op=clean_identifier(op, "unknown"), status=status)
        self.app.send({"type": "canopy.result", "id": call_id, "status": status, "body": body})
        if status == GONE_RESULT_STATUS:
            self.begin_stop(self._server_reason(body.get("reason") or body.get("detail"), REMOVED))

    def _on_beat(self, answer) -> None:
        if answer is RUN_GONE:
            self.last_server_ok = time.monotonic()
            self.begin_stop(REMOVED)
            return
        if answer is None:
            return
        self.last_server_ok = time.monotonic()
        self.record["last_server_ok_at"] = time.time()
        if answer.get("stop"):
            self.begin_stop(self._server_reason(answer.get("reason"), "server_stop"))

    @staticmethod
    def _server_reason(reason, default: str) -> str:
        # Canopy's own words, e.g. why a version was revoked; never an app's or a meeting's.
        text = " ".join(reason.split())[:200] if isinstance(reason, str) else ""
        return text or default

    # --- workers -------------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        while not self.done.wait(self.limits.heartbeat_every):
            try:
                answer = self.link.heartbeat(clean_identifier(self.run_state, "running"),
                                             self._heartbeat_counts())
            except server_mod.RunGone:
                answer = RUN_GONE
            except Exception:  # noqa: BLE001 — a failed beat is what the 5-minute clock is for
                answer = None
            self.inbox.put(("beat", "", answer))

    def _call_loop(self) -> None:
        while True:
            item = self.calls.get()
            if item is None:
                return
            call_id, op, args = item
            try:
                status, body = self.link.canopy(op, args)
            except Exception:  # noqa: BLE001 — an app must get an answer whatever went wrong
                status, body = server_mod.UNREACHABLE, {"detail": "could not reach canopy"}
            self.inbox.put(("result", "", (call_id, op, status, body)))

    # --- the loop ------------------------------------------------------------------

    def _start_children(self) -> bool:
        def launch(role: str, spec: ChildSpec) -> Optional[Child]:
            child = Child(role, spec, self.inbox, self.limits, self._on_stderr)
            child.start()
            child.send({"type": "start", "settings": spec.settings, "run_id": self.spec.run_id})
            return child

        try:
            if self.spec.source:
                self.source = launch("source", self.spec.source)
        except OSError:
            self.begin_stop("source_failed_to_start")
            return False
        try:
            self.app = launch("app", self.spec.app)
        except OSError:
            self.begin_stop("app_failed_to_start")
            return False
        return True

    def _check_exits(self, now: float) -> None:
        for child in (self.source, self.app):
            if child is None or child.proc is None:
                continue
            if child.exited_at is None and child.proc.poll() is not None:
                child.exited_at = now
            if child.exited_at is None:
                # Closed its stdout but stayed alive: it can never answer again.
                if child.eof and now - (child.eof_at or now) >= self.limits.kill_after:
                    child.kill()
                continue
            if child.exit_handled or not (child.eof or now - child.exited_at >= self.limits.exit_drain):
                continue
            child.exit_handled = True
            self._on_exit(child)

    def _on_exit(self, child: Child) -> None:
        code = child.proc.returncode
        self._log("exit", role=child.role, code=code)
        if self.stop_reason is not None:
            return
        if child.role == "app":
            self.begin_stop(self.app_said or (APP_EXITED if code == 0 else APP_CRASHED))
        elif self.source_ended_at is None:
            # A source that vanishes can't vouch for what it read: same as unreadable.
            self._relay_gone_then_stop(self.source_said or SOURCE_EXITED)

    def _check_clocks(self, now: float) -> None:
        if self.external_stop:
            self.begin_stop(self.external_stop)
        if self.stop_reason is not None:
            return
        if now - self.started >= self.spec.max_minutes * 60:
            self.begin_stop(MAX_MINUTES)
        elif self.link.beats and now - self.last_server_ok >= self.limits.unreachable_after:
            self.begin_stop(UNREACHABLE)
        elif (self.source_ended_at is not None
              and now - self.source_ended_at >= self.limits.ended_grace):
            self.begin_stop(SOURCE_ENDED)

    def _finished(self, now: float) -> bool:
        if self.stopping_at is None:
            return False
        running = [c for c in (self.app, self.source) if c and c.alive()]
        if not running:
            return True
        if now - self.stopping_at >= self.limits.kill_after:
            killed = []
            for child in running:
                if not child.killed:
                    child.kill()
                    killed.append(child.role)
            if killed:
                self._log("killed", roles=killed)
                self.record.setdefault("killed", []).extend(killed)
        return False

    def _handle(self, kind: str, role: str, payload) -> None:
        if kind == "line":
            self._on_line(role, payload)
        elif kind == "eof":
            child = self.app if role == "app" else self.source
            if child:
                child.eof = True
                child.eof_at = time.monotonic()
        elif kind == "beat":
            self._on_beat(payload)
        elif kind == "result":
            self._on_canopy_result(*payload)

    def _rotate_log(self) -> None:
        path = state.log_file(self.spec.key)
        try:
            if path.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass

    def run(self) -> str:
        state.ensure_dir(self.spec.key)
        if not self.lock.acquire():
            self._finish_server("already_running")
            return "already_running"
        self._rotate_log()
        self.started = self.last_server_ok = time.monotonic()
        self.record = {
            "key": self.spec.key, "version": self.spec.version, "run_id": self.spec.run_id,
            "pid": os.getpid(), "started_at": time.time(), "stopped_at": None,
            "stop_reason": None, "state": "starting", "unpublished": self.spec.unpublished,
            "last_event": "", "last_event_at": None, URL_FIELD: "", "killed": [],
        }
        self._save()
        self._log("started", version=clean_identifier(self.spec.version, "unknown"))

        threading.Thread(target=self._call_loop, daemon=True).start()
        if self.link.beats:
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        if self._start_children():
            self._save(children=[c.proc.pid for c in (self.source, self.app) if c])

        while True:
            now = time.monotonic()
            self._check_clocks(now)
            self._check_exits(now)
            if self._finished(now):
                break
            try:
                self._handle(*self.inbox.get(timeout=self.limits.tick))
                # Everything already waiting, before the clocks are read again.
                while True:
                    self._handle(*self.inbox.get_nowait())
            except queue.Empty:
                pass
        return self._wrap_up()

    def _wrap_up(self) -> str:
        self.done.set()
        self.calls.put(None)
        for child in (self.app, self.source):
            if child:
                try:
                    child.out_queue.put_nowait(None)
                except queue.Full:
                    pass
        self._log("stopped", reason=self.stop_reason, bad_lines=self.bad_lines, relayed=self.relayed)
        self._save(state="stopped", stopped_at=time.time(), stop_reason=self.stop_reason)
        # Released before the last call to the server: that can take seconds while offline,
        # and a person restarting the app shouldn't wait for it.
        self.lock.release()
        self._finish_server(self.stop_reason)
        return self.stop_reason

    def _finish_server(self, reason: str) -> None:
        try:
            self.link.finish(reason, self._heartbeat_counts())
        except Exception:  # noqa: BLE001 — the server notices a run with no heartbeats anyway
            pass


# Distinct from None, which means the beat got no usable answer.
RUN_GONE = object()


def _link_for(spec: RunSpec):
    if spec.unpublished:
        return server_mod.StubLink(spec.key)
    from ..client import Client
    from ..config import Config
    from ..runner_token import RunnerCredential

    cfg = Config.load(Path(spec.config))
    client = Client(cfg.base_url, RunnerCredential.for_config(cfg))
    return server_mod.ServerLink(server_mod.RunnerServer(client, cfg.runner_id), spec.run_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="supervise one local app run")
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    path = Path(args.spec)
    spec = RunSpec.from_json(json.loads(path.read_text(encoding="utf-8")))
    # Holds settings the person typed; the run has what it needs once it is read.
    path.unlink(missing_ok=True)
    host = Host(spec, _link_for(spec))
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: host.request_stop(STOPPED_BY_YOU))
    host.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
