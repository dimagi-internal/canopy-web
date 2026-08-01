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
    finish can't leave it pumping forever), interrupt the live session on a cancel,
    then tell the server. Best-effort — a client hiccup must not wedge the loop."""
    from . import cdp_control

    chat_bridge.IN_FLIGHT.pop(bridge.turn_id, None)
    CANCELLED_TURNS.discard(bridge.turn_id)
    if status == "cancelled":
        try:
            cdp_control.interrupt(bridge.task, port=cfg.cdp_port)
        except Exception as exc:  # noqa: BLE001 — cancel must still finish the turn
            logger.warning("chat turn=%s: interrupt failed: %s", bridge.turn_id, exc)
    try:
        client.finish(bridge.turn_id, note=note, status=status)
    except Exception:  # noqa: BLE001
        logger.warning("chat turn=%s: finish failed", bridge.turn_id, exc_info=True)
    logger.info("chat turn=%s %s (task=%s): %s", bridge.turn_id, status, bridge.task, note)


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
            finish_chat_bridge(cfg, client, bridge, status="cancelled", note="cancelled by user")
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
