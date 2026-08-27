"""Reporting the open emdash sessions this box can see.

The runner re-reports its WHOLE open-task set on a guaranteed cadence, because
the server treats that report as the liveness signal for a session: absence is a
direct observation rather than an inference. See SESSION_LIVE_WINDOW server-side."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import emdash, hooks, transcript
from .client import Client
from .config import Config
from .tail import TailReader

logger = logging.getLogger("canopy_runner")


_last_session_report = 0.0

# Per-session incremental tail readers, keyed by emdash_task — the byte-offset change
# signal that makes the phone reflect live emdash activity (see _session_changed).
_tail_readers: dict[str, "TailReader | None"] = {}

# Task names deleted by a `close_session` control frame, waiting to ride the next
# report's `archived:` list.
#
# Queued rather than POSTed on its own because the report is WHOLESALE: the server
# reconciles `archived` against the open set in ONE call, and `now_keys` must win
# (emdash task names are not unique, so an open task must never be retired by a
# closed namesake — apps/harness/services.py). Sending the closing signal separately
# would throw that ordering away.
_PENDING_CLOSED: set[str] = set()


# Set when something happened that the server must learn about NOW even though no
# transcript grew — today, the outcome of a human's tap on a blocked agent's
# dialog. A blocked session is silent by definition, so the change-driven tick
# never fires for it and the answer would wait out the whole heartbeat window.
_REPORT_NOW = False


def request_report_now() -> None:
    """Make the next tick report, whatever the change-check says."""
    global _REPORT_NOW
    _REPORT_NOW = True


def request_close_report(task_name: str) -> None:
    """Queue a deleted task's name for the next report's closing signal, and make
    that report happen on the very next tick rather than at the next heartbeat.

    Without this the row would wait out SESSION_LIVE_WINDOW (3 min) on absence
    alone — the latency the relay design exists to avoid.
    """
    _PENDING_CLOSED.add(task_name)


def session_changed(cfg: Config, sessions: list[dict]) -> bool:
    """True if any SHOWN session's transcript grew, or a new session appeared, since
    the last check. This is the LIVE signal — cheap (a byte-offset read of only the
    newly-appended bytes) and it catches assistant streaming too (transcript growth),
    not just user interaction (which is all last_interacted_at would catch)."""
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    active: set[str] = set()
    changed = False
    for s in sessions[: cfg.session_tail_count]:  # only the sessions the phone shows
        task = s.get("emdash_task")
        if not task:
            continue
        active.add(task)
        first_sight = task not in _tail_readers
        tr = _tail_readers.get(task)
        if tr is None:  # unresolved (new session, or transcript not found yet) — (re)try
            path = transcript.resolve_transcript(
                s.get("project") or "", task, home=home, claude_home=claude_home
            )
            tr = TailReader(str(path)) if path else None
            if tr is not None:
                tr.seek_end()  # stream only NEW activity from here, never the history
            _tail_readers[task] = tr
        if first_sight:
            changed = True  # a session newly appeared
        elif tr is not None and tr.read_new():
            changed = True  # its transcript grew
    for task in list(_tail_readers):  # drop readers for sessions that are gone
        if task not in active:
            _tail_readers.pop(task, None)
    return changed


# Per-task engine-flag watch: {emdash_task: (flag_value, baseline_mtime | None)}.
# `None` for the baseline means "settling" — see `annotate_engine_staleness`.
_ENGINE_FLAG: dict[str, "tuple[str, float | None]"] = {}

# How long after its last write a session that has ALREADY proved its engine flag
# wrong keeps claiming to be running.
#
# This is a de-latch, not a liveness heuristic: without it, a session that woke up
# from a background hand-off, worked, and then genuinely finished would stay marked
# running forever, because the second `Stop` writes the SAME flag value ("completed")
# that is already there and so never re-settles the baseline.
#
# Deliberately longer than the server's own 120s fallback window: the only sessions
# that reach this line have been observed writing after their flag said they were
# done, so the expensive mistake here is calling a working session finished — the very
# bug this exists to fix — not leaving a badge up three minutes too long on one that
# has stopped. A subagent appends to its transcript on every tool call, so real
# delegated work clears this bar continuously.
STILL_WRITING_SECONDS = 180


def annotate_engine_staleness(
    cfg: Config, sessions: list[dict], *, now_fn=time.time
) -> None:
    """Mark, in place, the sessions whose emdash flag says "not working" while the
    session is demonstrably STILL WRITING.

    emdash derives `agent_status` from three Claude Code hooks: UserPromptSubmit ->
    working, Stop -> completed, Notification -> awaiting-input. Claude Code fires
    `Stop` whenever the MAIN LOOP's turn ends — and a turn that ends only to hand off
    to a background subagent ends exactly the same way. What wakes the loop back up is
    a task-notification, not a UserPromptSubmit, so no `start` hook ever fires again:
    from the first background dispatch onward the flag is pinned at "completed" for
    the rest of the session. The server trusts a non-blank flag outright (it is
    normally the BETTER signal — see is_session_running), so a churning session reads
    as finished.

    Measured 2026-08-27 on `hh4`: background Agent dispatched 12:59:34, wake-ups at
    13:06 and 13:22, transcript still growing at 13:23:33 — emdash saying "completed"
    and the API saying running=False the whole time. The control case is fine and must
    stay fine: through a 200-second SILENT foreground tool call the flag correctly held
    "working", so this only ever looks at flags that already claim the session stopped.

    The discriminator is not recency — a real turn end is recent too — but writes that
    land AFTER the flag went non-working. The baseline is snapshotted one tick LATER
    than the flag change, so the turn's own closing write is inside the baseline rather
    than mistaken for new work.

    One-directional by construction: this can only ever say "still running", never
    "not running". A blank flag is left alone (the server has its own fallback for
    those) and a `working` flag needs no help.
    """
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    live: set[str] = set()
    for s in sessions[: cfg.session_tail_count]:
        task = s.get("emdash_task")
        if not task:
            continue
        live.add(task)
        status = str(s.get("agent_status") or "").strip()
        if not status or status == emdash.WORKING:
            _ENGINE_FLAG.pop(task, None)  # nothing to second-guess
            continue
        try:
            path = transcript.resolve_transcript(
                s.get("project") or "", task, home=home, claude_home=claude_home
            )
        except Exception:  # noqa: BLE001 — a fragile half must not cost us the report
            path = None
        wrote_at = transcript.activity_mtime(path)
        watched = _ENGINE_FLAG.get(task)
        if watched is None or watched[0] != status:
            _ENGINE_FLAG[task] = (status, None)  # new flag value: settle one tick
            continue
        baseline = watched[1]
        if baseline is None:
            _ENGINE_FLAG[task] = (status, wrote_at)  # settled — this is the mark
            continue
        if wrote_at > baseline and now_fn() - wrote_at <= STILL_WRITING_SECONDS:
            s["agent_status_stale"] = True
    for task in list(_ENGINE_FLAG):  # forget sessions that are gone
        if task not in live:
            _ENGINE_FLAG.pop(task, None)


def reported_projects(cfg: Config) -> list[str] | None:
    """The repos this box can drive, for the heartbeat to report — or None if we
    could not tell this tick.

    `capabilities["projects"]` is the allowlist `claim_next_turn` matches repo
    turns against. It used to be typed by a human at pairing and nothing kept it
    true, so a turn at `canopy` sat QUEUED forever while the repo sat in emdash's
    own projects table (labs, 2026-07-28). Now the box answers for itself, every
    tick, and the list cannot drift.

    None is NOT []. None means "unreadable — leave the server's list alone"; []
    means "I genuinely have none" and empties it. Collapsing the two would let one
    unreadable emdash DB blank the list and make every repo turn on this runner
    unclaimable — the same shape as the drift that once blanked the supervisor by
    reporting zero sessions, with more of the fleet behind it.
    """
    try:
        return emdash.list_projects(cfg.emdash_db)
    except emdash.EmdashReadError:
        # WARNING, not debug: the silent-degradation class verify-emdash exists
        # for. Omitting the field is safe; omitting it *quietly* is how a routing
        # outage ends up with no breadcrumb.
        logger.warning(
            "emdash project read FAILED — omitting the project report so the server keeps "
            "the list it already has. Run `canopy-runner verify-emdash` to check for "
            "schema drift.",
            exc_info=True,
        )
        return None
    except Exception:  # noqa: BLE001
        logger.debug("project list failed (non-fatal, omitting)", exc_info=True)
        return None


def maybe_report_sessions(cfg: Config, client: Client, now_fn=time.monotonic) -> None:
    """Report the open emdash sessions the phone can continue. CHANGE-DRIVEN: reports
    the instant a shown session's transcript grows (so the phone reflects live emdash
    activity within a poll tick), plus a heartbeat every session_report_seconds so a
    freshly-connected phone gets state. The cheap change-check runs every tick; the
    expensive recent-tail read + POST only on a real change or the heartbeat. A sqlite
    read of emdash's DB (runs even while CDP is down); best-effort — never stops a tick."""
    global _last_session_report
    try:
        sessions = emdash.list_open_sessions(cfg.emdash_db, cfg.session_report_limit)
    except emdash.EmdashReadError:
        # WARNING, not debug: this is the silent-degradation class verify-emdash
        # exists for. Skip the report entirely — an empty one would clear every
        # RunnerBinding server-side, which is the opposite of what we observed.
        logger.warning(
            "emdash session read FAILED — skipping this report so the server keeps the "
            "sessions it already knows. Run `canopy-runner verify-emdash` to check for "
            "schema drift.",
            exc_info=True,
        )
        return
    except Exception:  # noqa: BLE001
        logger.debug("session list failed (non-fatal)", exc_info=True)
        return
    # Every tick, not just reporting ones: the baseline this keeps is a
    # tick-over-tick comparison, so skipping ticks would coarsen it.
    try:
        annotate_engine_staleness(cfg, sessions)
    except Exception:  # noqa: BLE001
        logger.debug("engine-staleness annotation failed (non-fatal)", exc_info=True)
    global _REPORT_NOW
    changed = session_changed(cfg, sessions) or bool(_PENDING_CLOSED) or _REPORT_NOW
    _REPORT_NOW = False
    heartbeat = now_fn() - _last_session_report >= cfg.session_report_seconds
    if not changed and not heartbeat:
        return
    _last_session_report = now_fn()
    # Read the closing signal only on a tick we're actually going to report on.
    # Fail-soft in the opposite direction to the open-session read: losing the
    # archived list must not cost us the report, so omit the field and carry on.
    try:
        archived = emdash.list_recently_archived_tasks(
            cfg.emdash_db, cfg.session_report_limit
        )
    except emdash.EmdashReadError:
        logger.debug("archived-task read failed (non-fatal, omitting)", exc_info=True)
        archived = []
    # Snapshot rather than read-then-clear: `_PENDING_CLOSED` is written from the
    # wake-listener thread (close.close_session) and this runs on the report-tick
    # thread. A name added while the POST below is in flight must survive — a
    # wholesale `.clear()` after the call would drop it on the floor with no
    # fallback (emdash has already deleted the task, so nothing rediscovers it).
    # Same discard-only-the-snapshot shape as cancel.py::CANCELLED_TURNS.
    closing = set(_PENDING_CLOSED)
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        # The blocked-agent dialog, for EVERY reported session — the signal that
        # makes "the session stopped" answerable from a phone. Uncapped on
        # purpose (a waiting session sinks in an activity-ordered list, so a
        # top-K bound would hide the ones that have waited longest); see
        # `transcript.attach_pending_questions`.
        # Before attaching: a menu is persisted now, so it can outlive the emdash
        # task it describes. This report is the only thing that sees the whole
        # open set, which is what makes absence an observation rather than a
        # guess — the same property the server's liveness rule rests on.
        hooks.prune_menus(s.get("emdash_task") for s in sessions)
        transcript.attach_pending_questions(
            sessions, hook_menu_for=hooks.pending_hook_menu
        )
        client.report_sessions(cfg.runner_id, sessions, sorted(set(archived) | closing))
        # Discard only the names this report actually carried (mutate in place —
        # `_PENDING_CLOSED -= closing` would rebind the name, making it local
        # under Python's scoping rules and shadowing the module-level set). A
        # dropped POST must not lose the closing signal — the next tick retries
        # it (this snapshot stays queued), and re-reporting an already-retired
        # name is a no-op server-side.
        _PENDING_CLOSED.difference_update(closing)
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)

