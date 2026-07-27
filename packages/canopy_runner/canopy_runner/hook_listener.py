"""Loopback listener for Claude Code hook events.

Claude Code fires a hook per tool call; the hook `curl`s its JSON to this
listener, which maps it to a canopy Session and (when forwarding is on) ships it
as a live event. That gives the phone real-time tool activity, which transcript
tailing cannot: the Claude Code docs are explicit that the transcript "may lag
the in-memory conversation".

This is the same shape emdash already uses for its own hooks — a loopback POST
with a per-session nonce — so it is a pattern already proven on these machines
rather than a new one.

Three properties are load-bearing:

**It always answers 200, immediately.** A hook that errors or hangs is a hook
that can degrade the agent's own loop, so every failure path here still returns
success. Nothing observability does may cost an agent a turn.

**Forwarding is a switch, the listener is not.** `Config.forward_sessions` gates
only the POST to canopy-web. Off means events are accepted and dropped, so the
plumbing can be installed and watched before it carries anything.

**Live events are never persisted.** They carry `index = -1`, which the server
treats as view-only, so the durable record stays exactly one thing: the
transcript. That is what makes it safe for this path to drop events freely.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from canopy_transcript import events_for_hook

logger = logging.getLogger("canopy_runner.hooks")

# A hook body is one tool call — the largest realistic one is a big tool result,
# and the payloads measured on a live session were ~800 bytes. This is a sanity
# bound against a runaway body, not a working limit.
MAX_BODY_BYTES = 4 * 1024 * 1024


class HookListener:
    """Owns the loopback server and the cwd -> session mapping.

    `resolve_session(cwd)` is injected rather than imported so this module stays
    testable without the runner's client, binding cache, or a live server.
    """

    def __init__(self, *, port: int, nonce: str, resolve_session, forward):
        self.port = port
        self.nonce = nonce
        self._resolve_session = resolve_session
        self._forward = forward
        self._server: ThreadingHTTPServer | None = None
        self.received = 0
        self.forwarded = 0
        self.dropped_unknown_cwd = 0

    # -- request handling (pure enough to unit-test directly) ----------------

    def handle_payload(self, payload: dict) -> str:
        """Process one hook payload. Returns a short outcome string, for logs
        and tests. Never raises — the caller must answer 200 regardless."""
        self.received += 1
        try:
            events = events_for_hook(payload)
            if not events:
                return "ignored"          # not a forwarded event kind
            cwd = payload.get("cwd") or ""
            session_id = self._resolve_session(cwd)
            if not session_id:
                # Hooks are installed at USER level, so they fire for every
                # Claude Code session on this machine — including ones canopy
                # knows nothing about. Dropping them here is the expected path,
                # not an error.
                self.dropped_unknown_cwd += 1
                return "unknown-cwd"
            if not self._forward():
                return "not-forwarding"   # accepted and dropped, by configuration
            self._send(session_id, events)
            self.forwarded += 1
            return "forwarded"
        except Exception:  # noqa: BLE001 — a hook must never see a failure
            logger.debug("hook handling failed (non-fatal)", exc_info=True)
            return "error"

    def _send(self, session_id: str, events: list[dict]) -> None:
        raise NotImplementedError  # set by `bind_sender`

    def bind_sender(self, send) -> None:
        """Inject the transport (the runner's client) after construction."""
        self._send = send

    # -- server lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self.port <= 0 or self._server is not None:
            return
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                # Answer FIRST, work second: the hook's curl should never wait on
                # canopy-web, and its timeout should never become the agent's.
                token = self.headers.get("X-Canopy-Token", "")
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(length, MAX_BODY_BYTES)) if length else b""
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
                if token != listener.nonce:
                    logger.debug("hook rejected: bad nonce")
                    return
                try:
                    payload = json.loads(body or b"{}")
                except ValueError:
                    return
                if isinstance(payload, dict):
                    listener.handle_payload(payload)

            def log_message(self, *_args):  # silence BaseHTTPRequestHandler
                pass

        # Bound to 127.0.0.1 explicitly — never 0.0.0.0. This machine holds fleet
        # credentials; the listener must not be reachable off-box.
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="hook-listener").start()
        logger.info("hook listener on 127.0.0.1:%d (forwarding=%s)",
                    self.port, self._forward())

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None
