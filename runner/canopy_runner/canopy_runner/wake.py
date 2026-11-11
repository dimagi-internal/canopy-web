"""RC3 — a WebSocket wake-listener for the laptop runner.

A daemon thread holds a socket to canopy-web's runner control channel
(/ws/runner/{id}/) and sets `event` on every `wake` frame, so the poll loop can
claim IMMEDIATELY instead of waiting out poll_seconds. Deliberately additive: it
does NOT claim, heartbeat, or execute — the existing loop still owns all of that
(and remains the fallback). A wake we miss (socket down, no websocket-client
installed) only costs latency, never correctness — the loop's timeout still fires.

Kept stdlib-friendly: the one dependency (websocket-client) is imported lazily, so
the runner still starts (poll-only) if it isn't installed.
"""
from __future__ import annotations

import json
import logging
import threading
import time

from .runner_token import bearer_of

logger = logging.getLogger(__name__)


def ws_url(base_url: str, runner_id: str) -> str:
    b = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
    return f"{b}/ws/runner/{runner_id}/"


class WakeListener:
    def __init__(self, base_url: str, token, runner_id: str, *, recv_timeout: int = 30,
                 on_control=None):
        self.event = threading.Event()
        self._url = ws_url(base_url, runner_id)
        self._token = token
        self._recv_timeout = recv_timeout
        # Silence longer than this, pings unanswered included, means the socket is dead.
        self._dead_after = 2 * recv_timeout + 15
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_control = on_control
        # True while the socket is up. The poll loop only backs off when idle if
        # this holds: without the channel, the poll IS the only way work arrives.
        self.connected = False

    def _handle(self, raw: str) -> None:
        """Set the wake event on a `wake` frame; route other control frames (cancel,
        stream) to on_control; ignore malformed lines."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if msg.get("type") == "wake":
            self.event.set()
        elif self.on_control is not None and msg.get("type"):
            self.on_control(msg)

    def start(self) -> bool:
        """Begin listening. Returns False (poll-only) if websocket-client is absent."""
        try:
            import websocket  # noqa: F401
        except ImportError:
            logger.info("websocket-client not installed — wake listener off, polling only")
            return False
        self._thread = threading.Thread(target=self._run, name="wake-listener", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _serve(self, ws, websocket, clock=time.monotonic) -> None:
        """Read frames until the socket closes or goes silent.

        Silent is the case that matters. After the Mac sleeps or changes network, the
        socket can stay open on this side while the server has long forgotten it: no
        error, no close, just nothing, ever. Waiting on recv() alone, the listener
        believed it was connected for hours, so the loop never fell back to polling and
        every frame sent meanwhile (a phone's `listen_start`) was lost. So when idle it
        pings, and a socket that answers nothing, not even a pong, for `dead_after`
        seconds is given up and reopened."""
        last_heard = clock()
        while not self._stop.is_set():
            try:
                opcode, data = ws.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                if clock() - last_heard > self._dead_after:
                    logger.info("wake listener: nothing heard for %ds; reconnecting",
                                int(clock() - last_heard))
                    return
                ws.ping()
                continue
            last_heard = clock()
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                return
            if opcode == websocket.ABNF.OPCODE_TEXT:
                self._handle(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    self._url, header=[f"Authorization: Bearer {bearer_of(self._token)}"],
                    timeout=self._recv_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("wake ws connect failed (%s); retrying", exc)
                self._stop.wait(15)
                continue
            logger.info("wake listener connected: %s", self._url)
            self.connected = True
            try:
                self._serve(ws, websocket)
            except Exception as exc:  # noqa: BLE001 — a socket error is recoverable
                logger.debug("wake ws loop error (%s); reconnecting", exc)
            finally:
                self.connected = False
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(2)  # brief backoff before reconnect
