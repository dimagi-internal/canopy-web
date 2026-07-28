"""JSON-RPC transport to an ACP agent, over newline-delimited JSON.

Two layers, split so the protocol rules are testable without a Node subprocess:

- `AcpConnection` — framing, request/response correlation, agent->client
  request handling. Takes a pair of streams.
- `AcpAgent` — spawns `claude-agent-acp` and wires a connection to its stdio.

**Every agent->client request gets a response, always.** The agent BLOCKS on
`session/request_permission` and `fs/read_text_file`; a request we don't
implement, or one whose handler raises, still gets an error reply rather than
silence. One unanswered request wedges the session, and a wedged session holds
an EXECUTING turn until the lease sweep — a much worse failure than a refusal.

**A reader thread never dies.** Malformed lines and raising handlers are both
expected (an update handler is runner code posting over HTTP), and neither may
take the session down. This is the same rule the hook path follows: observability
must never cost an agent a turn.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading
from typing import Callable

logger = logging.getLogger("canopy_acp.client")

# JSON-RPC error code for "we don't implement that". Anything the agent might
# block on gets this rather than nothing.
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


class PermissionDecision:
    """What to answer a `session/request_permission` with.

    `option_id=None` means cancelled — an explicit refusal, which is still an
    answer. There is deliberately no "ignore": see the module docstring.
    """

    __slots__ = ("option_id",)

    def __init__(self, option_id: str | None):
        self.option_id = option_id

    def outcome(self) -> dict:
        if self.option_id is None:
            return {"outcome": "cancelled"}
        return {"outcome": "selected", "optionId": self.option_id}


class Pending:
    """A request in flight. `result()` blocks until the reply lands."""

    __slots__ = ("_event", "_value", "_error")

    def __init__(self):
        self._event = threading.Event()
        self._value = None
        self._error: str | None = None

    def _resolve(self, value) -> None:
        self._value = value
        self._event.set()

    def _fail(self, message: str) -> None:
        self._error = message
        self._event.set()

    def result(self, timeout: float | None = None):
        if not self._event.wait(timeout):
            raise TimeoutError("ACP request timed out")
        if self._error is not None:
            raise RuntimeError(self._error)
        return self._value


def allow_first_option(params: dict) -> PermissionDecision:
    """Default policy: take the first offered option.

    Right for a headless runner, where there is nobody to ask and a blocked turn
    is the failure. An interactive caller (the web chat answering a prompt)
    replaces this rather than editing it.
    """
    options = params.get("options") or []
    for option in options:
        if option.get("kind") in ("allow_always", "allow_once"):
            return PermissionDecision(option.get("optionId"))
    if options:
        return PermissionDecision(options[0].get("optionId"))
    return PermissionDecision(None)


class AcpConnection:
    """Newline-delimited JSON-RPC over a stream pair."""

    def __init__(self, *, write_to, read_from, permission_policy=None,
                 on_update: Callable[[str, dict], None] | None = None,
                 fs_root: pathlib.Path | None = None):
        self._out = write_to
        self._in = read_from
        self._next_id = 1
        self._pending: dict[int, Pending] = {}
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self.permission_policy = permission_policy or allow_first_option
        self.on_update = on_update
        # When set, fs/* requests are confined to this directory. The agent runs
        # in a worktree; a read outside it is a bug or worse.
        self.fs_root = pathlib.Path(fs_root).resolve() if fs_root else None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._reader is not None:
            return
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="acp-reader")
        self._reader.start()

    def close(self) -> None:
        """Shut the connection down without deadlocking the reader.

        **The read stream is deliberately not closed here.** The reader thread
        is blocked inside `readline()` on it, and closing a buffered reader from
        another thread waits for that reader to release the file's lock — which
        it cannot do until a line arrives. Caller and reader then wait on each
        other forever. Closing the WRITE end instead ends the agent's stdin, the
        adapter exits, and the reader gets a clean EOF and unwinds itself; the
        thread is a daemon, so a reader still parked on a stream nobody will
        write to never holds up shutdown either.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        # Fail every waiter rather than leaving a caller blocked on a reply that
        # can no longer arrive — a wedged waiter holds an EXECUTING turn open
        # until the lease sweep.
        with self._lock:
            pending, self._pending = self._pending, {}
        for waiter in pending.values():
            waiter._fail("ACP connection closed before a reply arrived")
        try:
            self._out.close()
        except Exception:  # noqa: BLE001 — closing must not raise
            pass

    # -- sending -----------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._closed.is_set():
            raise RuntimeError("ACP connection is closed")
        line = json.dumps(message) + "\n"
        with self._lock:
            self._out.write(line)
            self._out.flush()

    def request(self, method: str, params: dict | None = None) -> Pending:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            waiter = Pending()
            self._pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": params or {}})
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending.pop(request_id, None)
            waiter._fail(f"could not send {method}: {exc}")
        return waiter

    def notify(self, method: str, params: dict | None = None) -> None:
        """Fire-and-forget. `session/cancel` is a notification — sending it as a
        request would wait for a reply that never comes."""
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _respond(self, request_id, result) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception:  # noqa: BLE001
            logger.debug("could not answer agent request %s", request_id, exc_info=True)

    def _respond_error(self, request_id, message: str, code: int = INTERNAL_ERROR) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": code, "message": message}})
        except Exception:  # noqa: BLE001
            logger.debug("could not error agent request %s", request_id, exc_info=True)

    # -- receiving ---------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in self._in:
                if self._closed.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    # The adapter also writes diagnostics; a non-JSON line is
                    # noise, not a protocol failure.
                    logger.debug("non-JSON line from agent: %.120s", line)
                    continue
                try:
                    self._dispatch(message)
                except Exception:  # noqa: BLE001 — one bad message never ends the loop
                    logger.debug("dispatch failed", exc_info=True)
        except Exception:  # noqa: BLE001 — a closed pipe is an ordinary end
            logger.debug("ACP reader ended", exc_info=True)
        finally:
            self.close()

    def _dispatch(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        method = message.get("method")
        message_id = message.get("id")

        if method is None and message_id is not None:
            self._settle(message_id, message)
            return
        if method == "session/update":
            self._handle_update(message.get("params") or {})
            return
        if message_id is not None:
            self._handle_agent_request(message_id, method, message.get("params") or {})

    def _settle(self, message_id, message: dict) -> None:
        with self._lock:
            waiter = self._pending.pop(message_id, None)
        if waiter is None:
            return
        error = message.get("error")
        if error:
            waiter._fail(str(error.get("message") or error))
        else:
            waiter._resolve(message.get("result"))

    def _handle_update(self, params: dict) -> None:
        handler = self.on_update
        if handler is None:
            return
        try:
            handler(params.get("sessionId") or "", params.get("update") or {})
        except Exception:  # noqa: BLE001 — see module docstring
            logger.debug("update handler raised (non-fatal)", exc_info=True)

    def _handle_agent_request(self, message_id, method: str, params: dict) -> None:
        try:
            if method == "session/request_permission":
                decision = self.permission_policy(params)
                self._respond(message_id, {"outcome": decision.outcome()})
                return
            if method == "fs/read_text_file":
                self._respond(message_id, {"content": self._read_file(params)})
                return
            if method == "fs/write_text_file":
                self._write_file(params)
                self._respond(message_id, {})
                return
            # Terminals and anything else we haven't implemented: an explicit
            # error, never silence — the agent is blocked on this.
            self._respond_error(message_id, f"{method} is not implemented by this client",
                                METHOD_NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._respond_error(message_id, str(exc))

    def _resolve_path(self, params: dict) -> pathlib.Path:
        path = pathlib.Path(str(params.get("path") or "")).expanduser()
        if self.fs_root is not None:
            resolved = path.resolve()
            if not str(resolved).startswith(str(self.fs_root)):
                raise PermissionError(f"path outside the session root: {resolved}")
            return resolved
        return path

    def _read_file(self, params: dict) -> str:
        return self._resolve_path(params).read_text()

    def _write_file(self, params: dict) -> None:
        target = self._resolve_path(params)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(params.get("content") or ""))


# -- the adapter process ---------------------------------------------------

# `claude-agent-acp` is the adapter emdash itself runs. It is a Node package, so
# the runner needs node on PATH and the package resolvable — the cloud box
# installs both at bootstrap.
ADAPTER_PACKAGE = "@agentclientprotocol/claude-agent-acp"
ADAPTER_BIN = "claude-agent-acp"


def find_adapter(explicit: str | None = None) -> list[str] | None:
    """The argv that starts the adapter, or None if it isn't installed.

    Checked in order: an explicit path, a `claude-agent-acp` on PATH, then a
    local `node_modules` install next to the runner.
    """
    if explicit:
        path = pathlib.Path(explicit)
        if path.exists():
            return ["node", str(path)] if path.suffix == ".js" else [str(path)]
        return None
    on_path = shutil.which(ADAPTER_BIN)
    if on_path:
        return [on_path]
    node = shutil.which("node")
    if not node:
        return None
    for root in (pathlib.Path.cwd(), pathlib.Path(__file__).resolve().parent.parent):
        candidate = root / "node_modules" / ADAPTER_PACKAGE.replace("@", "").replace(
            "/", os.sep) / "dist" / "index.js"
        alt = root / "node_modules" / "@agentclientprotocol" / "claude-agent-acp" / "dist" / "index.js"
        for option in (alt, candidate):
            if option.exists():
                return [node, str(option)]
    return None


class AcpAgent:
    """A spawned `claude-agent-acp` plus its connection.

    Usage is deliberately explicit rather than a context manager with hidden
    lifecycle: the runner holds one of these across a whole turn and needs to
    interleave prompts, cancels and updates.
    """

    def __init__(self, *, cwd, argv: list[str] | None = None, env: dict | None = None,
                 on_update=None, permission_policy=None, confine_fs: bool = True):
        self.cwd = pathlib.Path(cwd)
        self._argv = argv or find_adapter(os.environ.get("ACP_ADAPTER_PATH"))
        if not self._argv:
            raise RuntimeError(
                f"{ADAPTER_PACKAGE} not found — install it (npm i -g {ADAPTER_PACKAGE}) "
                "or set ACP_ADAPTER_PATH")
        self._env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen | None = None
        self.conn: AcpConnection | None = None
        self._on_update = on_update
        self._permission_policy = permission_policy
        self._confine_fs = confine_fs
        self.session_id = ""

    def start(self) -> dict:
        """Spawn and `initialize`. Returns the agent's initialize result."""
        # stderr to a pipe we drain, not inherited: a chatty adapter must not
        # interleave with the runner's own logs.
        self.proc = subprocess.Popen(
            self._argv, cwd=str(self.cwd), env=self._env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._drain_stderr, daemon=True, name="acp-stderr").start()
        self.conn = AcpConnection(
            write_to=self.proc.stdin, read_from=self.proc.stdout,
            on_update=self._on_update, permission_policy=self._permission_policy,
            fs_root=self.cwd if self._confine_fs else None,
        )
        self.conn.start()
        return self.conn.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
        }).result(timeout=60)

    def _drain_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                logger.debug("acp stderr: %s", line.rstrip())
        except Exception:  # noqa: BLE001
            pass

    def new_session(self, timeout: float = 120) -> str:
        result = self.conn.request(
            "session/new", {"cwd": str(self.cwd), "mcpServers": []}).result(timeout=timeout)
        self.session_id = result.get("sessionId") or ""
        return self.session_id

    def load_session(self, session_id: str, timeout: float = 120) -> dict:
        """Resume an existing session — the ACP form of `--resume`.

        Replays the conversation as updates and appends to the SAME transcript
        file, so the durable record stays one file per session.
        """
        result = self.conn.request(
            "session/load",
            {"sessionId": session_id, "cwd": str(self.cwd), "mcpServers": []},
        ).result(timeout=timeout)
        self.session_id = session_id
        return result

    def prompt(self, text: str) -> Pending:
        """Send a prompt. Returns immediately — the caller decides whether to
        block, so a second prompt can be sent mid-turn (steering)."""
        return self.conn.request("session/prompt", {
            "sessionId": self.session_id,
            "prompt": [{"type": "text", "text": text}],
        })

    def cancel(self) -> None:
        """The Escape equivalent. A notification, not a request."""
        self.conn.notify("session/cancel", {"sessionId": self.session_id})

    def set_mode(self, mode_id: str) -> None:
        self.conn.request("session/set_mode",
                          {"sessionId": self.session_id, "modeId": mode_id})

    def close(self, timeout: float = 10) -> None:
        if self.conn is not None:
            self.conn.close()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
