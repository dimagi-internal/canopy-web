"""The runner's work, read through `GET /api/harness/runners/{id}/sync` when it moved.

canopy-web keeps one number per runner, its sync version (`apps/harness/runner_sync.py`),
that moves in the same transaction as every write that gives this box work: a stop, a
menu answer, a close, a backfill, a handoff, a stream a viewer wants. The version rides
the heartbeat reply, the socket's `heartbeat.ack` and every doorbell frame as `v`. The
box reads `/sync` (with its last ETag) only when:

- the `v` it saw differs from the version it last read,
- a doorbell frame arrived, or the control socket (re)connected,
- its own session report changed the set of sessions it holds (a new session's stream
  is a binding the server adds without moving the version), or
- RECONCILE_SECONDS passed: an unconditional read, so work whose doorbell and `v` were
  both missed, or that a handler could not finish, is delivered again.

The rows stay the truth and a frame stays a doorbell: `/sync` always answers every
pending row, never a diff.

How the drains use it (`Client.sync_*`): `streams` is the level list of sessions to
tail and is reused every tick; every other list (stops, answers, closes, backfills,
handoffs) is handed to its drain ONCE per answer, because a row the drain retires
(its result POST) is gone from the server without the version moving, and handing the
same answer again would press an answer twice. A row a drain could not finish comes
back on the next read.

A server without `/sync` (404/405) leaves the box on the per-tick drains, and the
route is asked again after UNSUPPORTED_RETRY_SECONDS.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("canopy_runner")

#: The unconditional read that catches anything a doorbell and `v` both missed.
RECONCILE_SECONDS = 300.0
#: How long a server that answered 404/405 is left on the per-tick drains.
UNSUPPORTED_RETRY_SECONDS = 1800.0

#: The lists `/sync` answers that a drain consumes once per answer.
EDGE_KEYS = ("interrupts", "answers", "closes", "backfills", "handoffs", "cancel_turn_ids")


class SyncBox:
    """What this box last read from `/sync`, and when to read it again."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        #: None until the first read; False while the server has no `/sync`.
        self.supported: bool | None = None
        self._unsupported_at = 0.0
        self.etag = ""
        self.version = -1
        self._read_at: float | None = None
        self._streams: list[dict] = []
        self._fresh: dict[str, list] = {}
        #: Highest `v` seen on a reply, ack or frame.
        self.seen_v = -1
        self._nudged = True  # the first tick reads

    # ---- what moves it --------------------------------------------------------

    def see(self, v) -> None:
        """Note a `v` from a heartbeat reply, an ack or a frame."""
        try:
            v = int(v)
        except (TypeError, ValueError):
            return
        with self._lock:
            if v > self.seen_v:
                self.seen_v = v

    def nudge(self) -> None:
        """A doorbell frame arrived, or the socket (re)connected: read on the next tick."""
        with self._lock:
            self._nudged = True

    def stale(self) -> None:
        """Something the version does not cover changed (this box's own session set):
        the next read is unconditional."""
        with self._lock:
            self._nudged = True
            self.etag = ""

    @property
    def active(self) -> bool:
        return self.supported is True

    # ---- reading -------------------------------------------------------------

    def due(self) -> tuple[bool, bool]:
        """(read now?, unconditionally?)."""
        now = self._clock()
        with self._lock:
            if self.supported is False:
                if now - self._unsupported_at < UNSUPPORTED_RETRY_SECONDS:
                    return False, False
                return True, True
            if self._read_at is None or now - self._read_at >= RECONCILE_SECONDS:
                return True, True
            if self._nudged or self.seen_v != self.version:
                return True, not self.etag
            return False, False

    def refresh(self, client, runner_id: str) -> bool:
        """Read `/sync` if due. True when a new answer arrived."""
        go, unconditional = self.due()
        if not go:
            return False
        try:
            status, etag, body = client.get_sync(runner_id, "" if unconditional else self.etag)
        except Exception as exc:  # noqa: BLE001 — a failed read keeps the last answer
            if getattr(exc, "status", None) in (404, 405):
                with self._lock:
                    if self.supported is not False:
                        logger.info("canopy-web has no /sync; draining each route per tick")
                    self.supported, self._unsupported_at = False, self._clock()
            else:
                logger.debug("sync read failed (non-fatal)", exc_info=True)
            return False
        now = self._clock()
        with self._lock:
            self.supported = True
            self._nudged = False
            if status == 304:
                # Unchanged: the answer in hand stands. A 304 to an unconditional read
                # cannot happen; this one was conditional, so the reconcile clock does
                # not restart on it.
                self.version = max(self.version, self.seen_v)
                return False
            body = body if isinstance(body, dict) else {}
            self.etag = etag or ""
            self.version = int(body.get("version") or 0)
            self.seen_v = max(self.seen_v, self.version)
            self._read_at = now
            self._streams = list(body.get("streams") or [])
            self._fresh = {k: list(body.get(k) or []) for k in EDGE_KEYS}
        return True

    # ---- what the drains read -------------------------------------------------

    def streams(self) -> list[dict]:
        with self._lock:
            return list(self._streams)

    def take(self, key: str) -> list:
        """The rows of `key` from the newest answer, once."""
        with self._lock:
            return self._fresh.pop(key, [])
