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

Two ways a stop arrives: the `session_interrupt` control frame (the doorbell,
`ring()`), and the server's durable record of it, `GET
/runners/{id}/session-interrupts`, read by `pull()` on the report sweep so a frame
lost on a down control channel costs a tick rather than the stop. Each carries the
stop's `interrupt_id`; once the stop lands or gives up, `drain()` retires it on the
server by that id.

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

#: {session_key: {"session_id": str, "attempts": int, "interrupt_ids": set}} —
#: stops rung and not yet confirmed. Guarded because ring() and take() run on
#: different threads.
_pending: dict[str, dict] = {}
#: Stop ids already carried out here, so the durable record read before the
#: server retired it is not rung a second time.
_done_ids: set[str] = set()
_lock = threading.Lock()


def ring(session_key: str, session_id: str = "", interrupt_id: str = "") -> None:
    """A stop arrived for this session. Called from the wake-listener thread, and
    by `pull()` for one recorded on the server.

    Re-ringing a session already pending does NOT reset its attempt count: a human
    jabbing the button three times is asking for the same stop, not for nine
    Escapes at a terminal that may since have moved on to other work.
    """
    if not session_key:
        return
    with _lock:
        if interrupt_id and interrupt_id in _done_ids:
            return
        entry = _pending.setdefault(session_key, {"session_id": session_id, "attempts": 0,
                                                  "interrupt_ids": set()})
        if session_id:
            entry["session_id"] = session_id
        if interrupt_id:
            entry["interrupt_ids"].add(interrupt_id)


def pull(cfg, client, runner_id: str) -> int:
    """Ring every stop the server holds for this runner that has not been rung —
    the stops whose frame never arrived. Returns how many were new. Network only;
    the Escape itself is `drain()`'s, on the next tick."""
    del cfg
    new = 0
    for item in client.sync_session_interrupts(runner_id):
        key, iid = str(item.get("session_key") or ""), str(item.get("interrupt_id") or "")
        if not key or not iid:
            continue
        with _lock:
            known = iid in _done_ids or iid in _pending.get(key, {}).get("interrupt_ids", ())
        if known:
            continue
        ring(key, str(item.get("session_id") or ""), iid)
        new += 1
    return new


def _retire(client, runner_id: str, session_id: str, ids: set[str], outcome: str) -> None:
    """Tell the server what became of these stops, which retires them there."""
    for iid in ids:
        with _lock:
            _done_ids.add(iid)
        if not session_id:
            continue
        try:
            client.post_session_interrupt_result(runner_id, session_id, iid, outcome)
        except Exception:  # noqa: BLE001 — the TTL retires it if this never lands
            logger.debug("session stop: could not retire %s", iid, exc_info=True)


def take() -> list[tuple[str, str]]:
    """The rung stops as (session_key, session_id), for this tick's attempt.

    `settle()` drops the ones that are finished with, so a stop that lands cleanly
    leaves no state behind and one that does not is retried a bounded number of
    times.
    """
    with _lock:
        return [(key, entry["session_id"]) for key, entry in _pending.items()]


def settle(session_key: str, *, confirmed: bool) -> tuple[bool, set[str]]:
    """Record this tick's outcome. Returns (whether the stop is now finished with —
    it landed, or used up its attempts — and the server ids it carried)."""
    with _lock:
        entry = _pending.get(session_key)
        if entry is None:
            return True, set()
        if confirmed or entry["attempts"] + 1 >= MAX_ATTEMPTS:
            _pending.pop(session_key, None)
            return True, set(entry.get("interrupt_ids") or ())
        entry["attempts"] += 1
        return False, set()


def drain(cfg, client, runner_id: str) -> int:
    """Press Escape for every session with a stop pending. Runs on the POLL thread.

    Returns the number confirmed stopped this tick. Never raises: this sits in the
    main loop next to the heartbeat, and a wedged emdash must not cost the runner
    its liveness.
    """
    from . import cdp_control, desktop

    confirmed_count = 0
    for session_key, session_id in take():
        try:
            if desktop.worktree_for(cfg, session_key) is not None:
                # A Claude desktop session: its mod aborts the running turn.
                res = desktop.stop(cfg, session_key)
            else:
                res = cdp_control.interrupt(session_key, port=cfg.cdp_port) or {}
            # A sidecar older than the verifying `interrupt` returns no `action`.
            # Unverified, NOT success — runner and sidecar update separately.
            action = res.get("action") or "unreadable"
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the loop
            logger.warning("session stop: interrupt failed for %s: %s", session_key, exc)
            action = "unreadable"

        confirmed = action in ("interrupted", "idle")
        done, ids = settle(session_key, confirmed=confirmed)
        if done and ids:
            _retire(client, runner_id, session_id, ids, action if confirmed else "failed")

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
