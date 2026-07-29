"""Scheduled-turn trigger: evaluate each schedule's cron locally and report the due
slot so the server materializes it as a normal turn. Runs in the runner loop — NO
Ada, NO LLM judgment in the hot path; "a slot came due → a turn" is a fixed rule.

The scheduler is a PRODUCER of turns, not a second execution engine: the runner's
existing poll is the tick, and a fired slot lands on the same claim → execute path
as an email- or manually-triggered turn. No celery, no beat, no deploy surface.

Three invariants this module exists to hold:

- **`fire_after`, never `last_slot`.** `due_slot(after=None)` looks backward with no
  lower bound, so a schedule created Wednesday would immediately owe the PREVIOUS
  Friday's slot — one that predates the schedule. The server computes
  `fire_after = last_slot or created_at` precisely so the runner can't get the
  fallback wrong; a schedule missing it is skipped rather than fired unbounded.
- **No backfill.** `due_slot` returns at most one slot by design. A laptop offline
  three weeks yields ONE occurrence (the newest), not three — the supersede rule
  applied at firing time: you only ever owe the latest report.
- **Firing is safe to race.** Both macOS-account runners may fire the same slot; the
  server's slot-derived idempotency_key collapses it. So there is deliberately no
  coordination, locking, or leader election here.
"""
from __future__ import annotations

import datetime as dt
import logging

from canopy_cron import due_slot

logger = logging.getLogger("canopy_runner")


def _parse_dt(raw: object) -> dt.datetime | None:
    """Parse a server ISO-8601 timestamp into an aware UTC datetime, or None."""
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.UTC)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def check_schedules(client, runner_id: str, *, now: dt.datetime,
                    paused: frozenset[str] | set[str] = frozenset()) -> dict:
    """Fire every schedule whose slot has come due. Returns
    {"fired": [{schedule_id, agent, name, slot, turn_id}], "failed": [schedule_ids]}.

    One schedule's failure — a bad cron, a 404, a flaky POST — never stops the rest:
    each is evaluated and fired independently, because a single broken row must not
    silence an entire fleet's cadence.
    """
    fired: list[dict] = []
    failed: list[int] = []
    for s in client.sync_schedules(runner_id):
        sid, agent = s.get("id"), s.get("agent_slug", "")
        # The server only serves enabled schedules (_runner_schedule_qs filters
        # enabled=True), but `enabled` is on the wire and a disabled schedule must
        # never fire — honor it here rather than trusting the peer to have filtered.
        if not s.get("enabled", True):
            continue
        # A paused agent burns no tokens: skip it exactly as the inbox trigger does,
        # or resuming would stampede every slot queued while it was paused.
        if agent in paused:
            continue
        after = _parse_dt(s.get("fire_after"))
        if after is None:
            # Never fall back to `after=None`: unbounded lookback fires a slot that
            # predates the schedule. A missing anchor is a server/contract problem.
            logger.warning("schedule[%s] '%s': no fire_after anchor — skipping (never "
                           "fire unbounded)", agent, s.get("name", ""))
            failed.append(sid)
            continue
        try:
            slot = due_slot(s.get("cron", ""), s.get("timezone", ""), after=after, now=now)
        except ValueError as exc:
            # Bad cron/tz survived server validation somehow — name it, skip it.
            logger.warning("schedule[%s] '%s': %s — skipping", agent, s.get("name", ""), exc)
            failed.append(sid)
            continue
        if slot is None:
            continue  # nothing due — the common case, deliberately silent
        try:
            turn = client.fire_schedule(sid, runner_id, slot.isoformat())
        except Exception as exc:  # noqa: BLE001 — one bad schedule never stops the others
            logger.warning("schedule[%s] '%s': fire for slot %s failed: %s",
                           agent, s.get("name", ""), slot.isoformat(), exc)
            failed.append(sid)
            continue
        turn_id = (turn or {}).get("id", "")
        # A fire is a notable event — one line, always. (A same-slot race with the
        # other macOS account's runner collapses server-side, so this may name a turn
        # the peer created; both are success and neither needs reconciling.)
        logger.info("schedule[%s] '%s': slot %s due — FIRED turn %s",
                    agent, s.get("name", ""), slot.isoformat(), turn_id)
        fired.append({"schedule_id": sid, "agent": agent, "name": s.get("name", ""),
                      "slot": slot.isoformat(), "turn_id": turn_id})
    return {"fired": fired, "failed": failed}
