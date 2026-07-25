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

logger = logging.getLogger(__name__)


def ws_url(base_url: str, runner_id: str) -> str:
    b = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
    return f"{b}/ws/runner/{runner_id}/"


class WakeListener:
    def __init__(self, base_url: str, token: str, runner_id: str, *, recv_timeout: int = 30,
                 on_control=None):
        self.event = threading.Event()
        self._url = ws_url(base_url, runner_id)
        self._token = token
        self._recv_timeout = recv_timeout
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_control = on_control

    def _handle(self, raw: str) -> None:
        """Set the wake event on a `wake` frame; route other control frames (cancel,
        interject, stream) to on_control; ignore malformed lines."""
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

    def _run(self) -> None:
        import websocket

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    self._url, header=[f"Authorization: Bearer {self._token}"],
                    timeout=self._recv_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("wake ws connect failed (%s); retrying", exc)
                self._stop.wait(15)
                continue
            logger.info("wake listener connected: %s", self._url)
            try:
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue  # idle; keep the socket open
                    if not raw:
                        break
                    self._handle(raw)
            except Exception as exc:  # noqa: BLE001 — a socket error is recoverable
                logger.debug("wake ws loop error (%s); reconnecting", exc)
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            self._stop.wait(2)  # brief backoff before reconnect
