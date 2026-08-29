"""Driving a chat turn to completion across ticks.

A chat turn OUTLIVES the tick that started it: `execute_chat_turn` registers a
bridge and this pumps it once per tick, so the runner keeps heartbeating,
claiming and reporting while an agent works. It finishes when the transcript
says the agent handed the floor back — NOT when the file goes quiet, which used
to end every turn at its first long tool call."""
from __future__ import annotations

import logging
import time

from . import chat_bridge
from .cancel import CANCELLED_TURNS
from .client import Client
from .config import Config
from .failure_log import note_failure, note_success

logger = logging.getLogger("canopy_runner")


def _is_blocked(task: str) -> bool:
    """Whether a dialog is currently up on this emdash task.

    Keyed by task across projects for the same reason `note_answer_outcome` is:
    the bridge knows the emdash task it is tailing, not the project, and the
    answer is only ever used as a "do not call this wedged" veto.
    """
    from . import hooks

    listener = hooks._hook_listener
    if listener is None or not task:
        return False
    try:
        return any(k[1] == task for k in listener._pending_menus)
    except Exception:  # noqa: BLE001 — a bridge tick must never die on this
        return False


def finish_chat_bridge(cfg: Config, client: Client, bridge, *, status: str, note: str) -> None:
    """Retire one in-flight bridge: drop it from the registry FIRST (so a failing
    finish can't leave it pumping forever), then tell the server. Best-effort — a
    client hiccup must not wedge the loop.

    Pressing Escape is NOT done here any more; see `cancel_chat_bridge`. It used to
    be, and a cancel arrived at this function already committed to finishing the turn
    `cancelled` — so an interrupt that raised was caught, logged at WARNING, and the
    turn was reported cancelled anyway. The website said "stopped" while the agent
    kept working, and a test asserted that behaviour."""
    chat_bridge.IN_FLIGHT.pop(bridge.turn_id, None)
    CANCELLED_TURNS.discard(bridge.turn_id)
    try:
        client.finish(bridge.turn_id, note=note, status=status, emdash_task_id=bridge.task)
    except Exception:  # noqa: BLE001
        logger.warning("chat turn=%s: finish failed", bridge.turn_id, exc_info=True)
    logger.info("chat turn=%s %s (task=%s): %s", bridge.turn_id, status, bridge.task, note)


# How many ticks a stop may spend trying before we report what actually happened.
# Each attempt is itself two Escapes with a repaint wait inside the sidecar, so this
# is seconds, not minutes — the human is watching a button they just pressed.
CANCEL_MAX_ATTEMPTS = 3


def cancel_chat_bridge(cfg: Config, client: Client, bridge) -> None:
    """Act on a stop for one in-flight bridge: interrupt the live session, and finish
    the turn ONLY once we know what the interrupt did.

    The outcomes are the sidecar's, and they are not all "cancelled":

    * `interrupted` / `idle` — the agent is not running. Finish CANCELLED; true.
    * `still-running`        — Escape did not take. Retried across ticks, then the
                               turn finishes FAILED, because a turn reported
                               cancelled while its agent runs on is the one outcome
                               a stop button must never produce. The note names it.
    * `unreadable` / raised  — we could not see. Retried, then CANCELLED with the
                               uncertainty IN the note: a false red is a wrong answer
                               too, and "emdash is gone" genuinely does end the turn.

    Retrying leaves the bridge in flight for a tick, which pauses streaming for that
    tick. That is the right trade: the human asked for it to stop.
    """
    from . import cdp_control

    try:
        res = cdp_control.interrupt(bridge.task, port=cfg.cdp_port) or {}
        # A sidecar older than this code returns no `action`. Unverified, NOT success:
        # runner and sidecar update separately, so that version will be live here.
        action = res.get("action") or "unreadable"
        err = ""
    except Exception as exc:  # noqa: BLE001 — a stop must still reach a verdict
        action, err = "unreadable", str(exc)[:200]

    if action in ("interrupted", "idle"):
        note = "cancelled by user" if action == "interrupted" else (
            "cancelled by user (the agent had already stopped)")
        finish_chat_bridge(cfg, client, bridge, status="cancelled", note=note)
        return

    bridge.cancel_attempts += 1
    if bridge.cancel_attempts < CANCEL_MAX_ATTEMPTS:
        logger.warning("chat turn=%s: stop attempt %d/%d on task=%s did not confirm (%s%s)",
                       bridge.turn_id, bridge.cancel_attempts, CANCEL_MAX_ATTEMPTS,
                       bridge.task, action, f": {err}" if err else "")
        return  # stays in flight; CANCELLED_TURNS still holds the id, so we retry next tick

    if action == "still-running":
        note = (f"stop did not take: Escape was pressed {CANCEL_MAX_ATTEMPTS} times and "
                f"'{bridge.task}' is still running — the agent may still be working")
        # SAY IT ON THE CHANNEL THE HUMAN IS WATCHING. A turn's terminal `status` and
        # its result_note carry no client-visible frame (stream_map.turn_event_to_frames:
        # "status / heartbeat / question / approval carry no client-visible stream
        # frame"), so finishing `failed` alone would just make the reply stop — which
        # is what a successful stop looks like too. An `error` event renders as
        # chat.stream_error, which is the only way this fix reaches the person who
        # pressed the button.
        try:
            client.post_events(bridge.turn_id, [{"kind": "error", "payload": {"detail": note}}])
        except Exception:  # noqa: BLE001 — telling them is best-effort; finishing is not
            logger.warning("chat turn=%s: could not report the failed stop to the client",
                           bridge.turn_id, exc_info=True)
        finish_chat_bridge(cfg, client, bridge, status="failed", note=note)
    else:
        finish_chat_bridge(
            cfg, client, bridge, status="cancelled",
            note=("cancelled by user (interrupt could not be verified"
                  + (f": {err}" if err else "") + ")"))


def pump_chat_bridges(cfg: Config, client: Client) -> None:
    """Advance every in-flight chat turn by one tick: ship whatever the agent has
    written since the last tick, and finish the turn once the transcript says it
    handed the floor back (see chat_bridge.hands_back_to_human).

    This is why a chat turn no longer blocks the loop. It used to run inline inside
    execute_chat_turn, which was only survivable because it gave up after 3 seconds
    of transcript silence — and giving up after 3s is exactly what truncated every
    answer that involved a tool call. Pumping it here buys the correct completion
    rule without holding the tick: the heartbeat, claims and session reports all
    keep running while an agent works.
    """
    for turn_id, bridge in list(chat_bridge.IN_FLIGHT.items()):
        if turn_id in CANCELLED_TURNS:
            cancel_chat_bridge(cfg, client, bridge)
            continue
        try:
            new_records = bridge.reader.read_new()
            raw_lines = list(getattr(bridge.reader, "last_raw", ()) or ())
        except Exception:  # noqa: BLE001 — an unreadable transcript is a quiet tick
            logger.debug("chat turn=%s: transcript read failed", turn_id, exc_info=True)
            new_records, raw_lines = [], []
        # A dialog up on this session means "waiting on a human", not "wedged" —
        # see LiveBridge.step. Read from the hook listener, so it costs nothing
        # and never touches emdash.
        blocked = _is_blocked(bridge.task)
        bridge.step(new_records, raw_lines, blocked=blocked)
        flush_turn_transcript(client, bridge)
        if bridge.pending:
            events = [{"kind": "assistant", "payload": {"text": t}} for t in bridge.pending]
            try:
                client.post_events(turn_id, events)
                bridge.pending.clear()   # only drop text the server has actually taken
            except Exception:  # noqa: BLE001 — keep it queued and retry next tick
                logger.debug("chat turn=%s: event post failed, retrying", turn_id, exc_info=True)
        if bridge.finished:
            # One last attempt before the turn closes: whatever the agent wrote in
            # its final tick is exactly the part a cost aggregator needs (the
            # `result` line carries the turn's totals).
            flush_turn_transcript(client, bridge, final=True)
            finish_chat_bridge(cfg, client, bridge, status="done", note=bridge.note)


def flush_turn_transcript(client: Client, bridge, *, final: bool = False) -> None:
    """Ship the bridge's accumulated raw JSONL to the turn's retained transcript.

    Best-effort and non-blocking for the turn: the reply is the product, this is
    a derived artifact. A failed batch stays queued and is retried next tick; on
    the FINAL flush an unshippable batch is dropped rather than holding the turn
    open, and says so in the log.

    Batches carry `<turn>:<n>` as their batch_id so a lost-ack retry dedupes
    server-side instead of double-appending (apps/harness/services.append_transcript).
    """
    if bridge.transcript_truncated or not bridge.raw_pending:
        return
    for batch in chat_bridge.chunk_raw_lines(bridge.raw_pending):
        batch_id = f"{bridge.turn_id}:{bridge.raw_batches_sent}"
        try:
            still_open = client.post_transcript(bridge.turn_id, batch, batch_id)
        except Exception:  # noqa: BLE001
            if final:
                note_failure(f"transcript:{bridge.turn_id}",
                              f"final transcript flush ({len(batch)} lines, dropped)")
                bridge.raw_pending.clear()
            else:
                note_failure(f"transcript:{bridge.turn_id}", "transcript flush")
            return
        bridge.raw_batches_sent += 1
        del bridge.raw_pending[:len(batch)]
        if not still_open:
            # Per-turn ceiling reached; every further batch is a server-side
            # no-op, so stop paying for them.
            bridge.transcript_truncated = True
            bridge.raw_pending.clear()
            logger.info("chat turn=%s: transcript ceiling reached; no further flushes",
                        bridge.turn_id)
            return
    note_success(f"transcript:{bridge.turn_id}")


def drain_chat_bridges(cfg: Config, client: Client, *, poll: float = 1.0,
                        max_seconds: float = 3600.0) -> None:
    """Pump to completion, for the ONE-SHOT modes (--once / --drain-one) that exit
    when they return. The daemon never calls this — it pumps on its own ticks. The
    process would otherwise leave the turn EXECUTING until the server's lease sweep
    reclaimed it, so a `--drain-one` chat turn would never deliver its reply."""
    deadline = time.monotonic() + max_seconds
    last_hb = time.monotonic()
    while chat_bridge.IN_FLIGHT and time.monotonic() < deadline:
        pump_chat_bridges(cfg, client)
        if not chat_bridge.IN_FLIGHT:
            return
        if time.monotonic() - last_hb >= 60:
            # Renew the turn lease: nothing else heartbeats in a one-shot run, and a
            # long reply would otherwise outlive it and be swept mid-answer.
            try:
                client.heartbeat(cfg.runner_id, sorted(chat_bridge.IN_FLIGHT))
            except Exception:  # noqa: BLE001
                logger.debug("drain heartbeat failed (non-fatal)", exc_info=True)
            last_hb = time.monotonic()
        time.sleep(poll)


# Persistent per-key failure counters for the best-effort transcript paths
# (stream posts, backfill posts). These retry every tick forever by design, so
# logging each failure would spam and logging none is what actually happened:
# the NUL-byte bug (2026-07-26) was a hard 500 on every backfill attempt for one
# session, invisible in the runner log because the handler logged at DEBUG. A
