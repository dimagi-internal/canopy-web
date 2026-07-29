"""Reporting the open emdash sessions this box can see.

The runner re-reports its WHOLE open-task set on a guaranteed cadence, because
the server treats that report as the liveness signal for a session: absence is a
direct observation rather than an inference. See SESSION_LIVE_WINDOW server-side."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import emdash, transcript
from .client import Client
from .config import Config
from .tail import TailReader

logger = logging.getLogger("canopy_runner")


_last_session_report = 0.0

# Per-session incremental tail readers, keyed by emdash_task — the byte-offset change
# signal that makes the phone reflect live emdash activity (see _session_changed).
_tail_readers: dict[str, "TailReader | None"] = {}


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
    changed = session_changed(cfg, sessions)
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
    try:
        transcript.attach_recent_tail(
            sessions, count=cfg.session_tail_count, limit=cfg.session_tail_limit
        )
        client.report_sessions(cfg.runner_id, sessions, archived)
    except Exception:  # noqa: BLE001
        logger.debug("session report failed (non-fatal)", exc_info=True)

