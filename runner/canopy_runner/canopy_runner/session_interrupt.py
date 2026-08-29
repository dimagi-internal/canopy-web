"""Stops asked of a SESSION rather than of a turn.

The turn-shaped cancel (`cancel.CANCELLED_TURNS` -> `chat_pump.cancel_chat_bridge`)
can only reach work a live Turn still owns, which in practice means chat. An agent,
board or scheduled turn is fire-and-continue: `execute_turn` finishes it the moment
the prompt is delivered, so seconds later the agent is working hard on a turn that
is already DONE and nothing turn-shaped can reach it. Those sessions were simply
unstoppable from the web.

They are sessions, though, and canopy knows the session: every agent/project/phone
thread gets a durable Session plus a RunnerBinding carrying `session_key` — the emdash
task. So a stop is addressed exactly the way a menu answer already is.

THE THREAD SPLIT IS THE POINT, and it is why this is a doorbell rather than a
function the socket calls. `ring()` runs on the WAKE-LISTENER thread, which also
carries `cancel`, `wake` and `menu_answer`; a CDP round trip there would block the
socket that keeps this runner alive for however long emdash takes to answer. So the
frame only marks work due and the POLL thread does it, the same rule `inbox_due`
follows for the same reason.

Retries live here too. A single Escape is not proof (see cdp_control.interrupt): the
sidecar reports what it saw, and an unconfirmed stop is worth trying again on the
next tick rather than being declared done or declared failed on one look.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("canopy_runner")

#: How many ticks an unconfirmed stop keeps trying before we give up on it. Mirrors
#: chat_pump.CANCEL_MAX_ATTEMPTS — the same question, asked about a session.
MAX_ATTEMPTS = 3

#: {session_key: {"session_id": str, "attempts": int}} — stops rung and not yet
#: confirmed. Guarded because ring() and take() run on different threads.
_pending: dict[str, dict] = {}
_lock = threading.Lock()


def ring(session_key: str, session_id: str = "") -> None:
    """A stop arrived for this session. Called from the wake-listener thread.

    Re-ringing a session already pending does NOT reset its attempt count: a human
    jabbing the button three times is asking for the same stop, not for nine
    Escapes at a terminal that may since have moved on to other work.
    """
    if not session_key:
        return
    with _lock:
        entry = _pending.setdefault(session_key, {"session_id": session_id, "attempts": 0})
        if session_id:
            entry["session_id"] = session_id


def take() -> list[tuple[str, str]]:
    """Drain the rung stops as (session_key, session_id), for this tick's attempt.

    Draining rather than peeking: `settle()` puts back the ones still unconfirmed,
    so a stop that lands cleanly leaves no state behind and one that does not is
    retried a bounded number of times.
    """
    with _lock:
        return [(key, entry["session_id"]) for key, entry in _pending.items()]


def settle(session_key: str, *, confirmed: bool) -> bool:
    """Record this tick's outcome. Returns whether the stop is now finished with —
    either because it landed, or because it has used up its attempts."""
    with _lock:
        entry = _pending.get(session_key)
        if entry is None:
            return True
        if confirmed:
            _pending.pop(session_key, None)
            return True
        entry["attempts"] += 1
        if entry["attempts"] >= MAX_ATTEMPTS:
            _pending.pop(session_key, None)
            return True
        return False


def drain(cfg, client, runner_id: str) -> int:
    """Press Escape for every session with a stop pending. Runs on the POLL thread.

    Returns the number confirmed stopped this tick. Never raises: this sits in the
    main loop next to the heartbeat, and a wedged emdash must not cost the runner
    its liveness.
    """
    from . import cdp_control

    confirmed_count = 0
    for session_key, session_id in take():
        try:
            res = cdp_control.interrupt(session_key, port=cfg.cdp_port) or {}
            # A sidecar older than the verifying `interrupt` returns no `action`.
            # Unverified, NOT success — runner and sidecar update separately.
            action = res.get("action") or "unreadable"
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the loop
            logger.warning("session stop: interrupt failed for %s: %s", session_key, exc)
            action = "unreadable"

        confirmed = action in ("interrupted", "idle")
        done = settle(session_key, confirmed=confirmed)

        # Say what happened, on both outcomes. Only the SUCCESS was reported before,
        # which left a failed stop indistinguishable from a stop nobody asked for —
        # the person who pressed the button watched a session that just went on
        # working with no explanation. Two independent facts go up:
        #
        #   activity:  what the AGENT is doing. `idle` only when confirmed. Never
        #              touched on failure, because it is genuinely still working.
        #   stop:      whether the STOP took. This is the one that was missing.
        events = []
        if confirmed:
            confirmed_count += 1
            events.append({"kind": "activity:idle", "seq": -1, "index": -1, "payload": {}})
            events.append({"kind": "stop:stopped", "seq": -1, "index": -1, "payload": {}})
        elif done:
            events.append({"kind": "stop:failed", "seq": -1, "index": -1,
                           "payload": {"attempts": MAX_ATTEMPTS, "reason": action}})
        if events and session_id:
            try:
                client.post_session_stream(runner_id, session_id, events)
            except Exception:  # noqa: BLE001 — reporting is best-effort, stopping is not
                logger.debug("session stop: could not report the outcome for %s",
                             session_id, exc_info=True)

        if confirmed:
            logger.info("session stop: %s interrupted (%s)", session_key, action)
        elif done:
            logger.warning("session stop: giving up on %s after %d attempts (%s) — "
                           "the agent may still be working",
                           session_key, MAX_ATTEMPTS, action)
    return confirmed_count
