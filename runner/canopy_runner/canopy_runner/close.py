"""Closing a session from the web: delete the emdash task, then TELL the server.

The server wrote NOTHING when it relayed the close — for a local session the
emdash task is the truth, and `replace_reported_sessions` would un-archive a
server-side write within ~10s anyway. So this module's report is the answer:
`sessions.request_close_report` puts the task name on the next report's
`archived:` list, which apps/harness/services.py turns into status=ARCHIVED.
Absence alone would say the same thing three minutes later.

The consequence of writing nothing server-side is that a FAILED close needs no
cleanup: the task keeps being reported, the row stays, and the list is telling
the truth. Signal only what actually happened.

What a failed close DOES need is its reason in canopy: callers turn the
exception into a sentence (`failure_reason`) and send it (`report_results`), so
Supervisor says "Close failed: <reason>" beside the session that stayed open.
"""
from __future__ import annotations

import logging
import threading

from . import cdp_control, desktop, emdash, session_target, sessions

logger = logging.getLogger(__name__)


class CloseRefused(Exception):
    """The session could not be placed in exactly one emdash project, so it was
    NOT deleted. A delete is irreversible; by name alone it can take another
    agent's same-named session (ada's and eva's "editing", 2026-10-04).

    `reason` is session_target's word for why (ambiguous / unknown / …)."""

    def __init__(self, message: str = "", *, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class CloseFailed(Exception):
    """The delete was attempted and did not take: the task is still in emdash."""


#: What a person sees in Supervisor for a session the runner would not close
#: because it cannot place it in one project (including one with no project).
NO_PROJECT = "couldn't tell which project's task this is"

_REFUSAL_DETAIL = {
    session_target.AMBIGUOUS: "several projects have a task by this name",
    session_target.UNKNOWN: "emdash's database could not be read",
}


def failure_reason(exc: BaseException, *, cdp_port: int = 9222) -> str:
    """A failed close, in a short sentence a person can act on.

    It is shown in Supervisor next to the session ("Close failed: <reason>"),
    so it names the thing to go and fix rather than a stack trace."""
    if isinstance(exc, CloseRefused):
        detail = _REFUSAL_DETAIL.get(getattr(exc, "reason", ""), "")
        return f"{NO_PROJECT} ({detail})" if detail else NO_PROJECT
    if isinstance(exc, CloseFailed):
        return "emdash still has this task, but its sidebar did not show it to delete"
    msg = " ".join(str(exc).split())
    if isinstance(exc, cdp_control.CDPError):
        if not cdp_control.cdp_healthy(port=cdp_port):
            return f"emdash unreachable over CDP (port {cdp_port})"
        if "timed out" in msg:
            return "emdash did not answer over CDP in time"
        if "node not found" in msg:
            return "Node.js is not installed for the runner"
        return f"emdash: {msg}"[:200] if msg else "emdash refused the delete"
    return (msg or type(exc).__name__)[:200]


def outcome(session_key: str, project: str, error: str = "") -> dict:
    """One close result as `report_results` sends it: ok when there is no error.
    The single shape every close path reports."""
    return {"session_key": session_key, "project": project, "ok": not error, "error": error}


def report_results(cfg, client, results: list[dict]) -> None:
    """Send close outcomes (`{session_key, project, ok, error}`) to canopy, so a
    failure is visible in Supervisor and a success clears an earlier one.

    Best-effort: a report that cannot be sent costs the visibility, never the
    close. Callers have already logged each failure."""
    if not results or client is None:
        return
    try:
        client.post_close_results(cfg.runner_id, results)
    except Exception:  # noqa: BLE001
        logger.warning("could not report %d close result(s) to canopy", len(results),
                       exc_info=True)


# Keys already warned about, so a refusal retried every poll tick logs once.
_warned: set[str] = set()

# Closes under way, by task name. A close from the web arrives twice — the
# control frame (wake-listener thread) and `close_requested` on the next poll
# tick (streams.drain_closes) — and the second used to run alongside the first:
# its sidebar search missed the row the first was deleting while emdash's DB
# still held it, so it reported CloseFailed for a close that then succeeded.
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()

#: What close_session returns when another thread is already closing the task.
IN_FLIGHT = "in-flight"


def settled_or_in_flight(session_key: str) -> bool:
    """Whether this box is closing `session_key` right now, or has closed it and
    is waiting for the next report to say so. Either way there is nothing for
    another attempt to do, and an attempt now can only race the first."""
    with _IN_FLIGHT_LOCK:
        return session_key in _IN_FLIGHT or sessions.close_report_pending(session_key)


def close_session(session_key: str, *, project: str = "", cdp_port: int = 9222,
                  emdash_db: str | None = None, cfg=None) -> str:
    """Delete `session_key`'s emdash task — the one under `project` — and queue its
    closing signal.

    Returns the CDP action — "deleted", or "absent" when the task was already gone
    (a double-tap, or a human who deleted it in emdash a moment earlier). Both
    queue the signal: the task is gone either way, and the server may not know.
    A Claude desktop session (pass `cfg` to recognise one) is closed by its own
    runtime and returns "closed" (see desktop.close).

    Raises CloseRefused, touching nothing, when the project cannot be resolved
    (see session_target) — never a delete by name alone. Raises CDPError if the
    delete could not be completed, and CloseFailed if the sidebar called
    the task absent while emdash's DB still holds it. The caller logs either and moves on.

    Returns IN_FLIGHT, touching nothing, while another thread is closing the same
    task: two deletes at once make the loser report a failure that is not one.
    """
    with _IN_FLIGHT_LOCK:
        if session_key in _IN_FLIGHT:
            logger.info("close %s: already being closed — not starting a second", session_key)
            return IN_FLIGHT
        _IN_FLIGHT.add(session_key)
    try:
        return _close_session(session_key, project=project, cdp_port=cdp_port,
                              emdash_db=emdash_db, cfg=cfg)
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(session_key)


def _close_session(session_key: str, *, project: str, cdp_port: int,
                   emdash_db: str | None, cfg) -> str:
    if cfg is not None and desktop.is_desktop_session(cfg, session_key):
        # A Claude desktop session has no emdash task: it is open for as long as the
        # runner's desktop index names it (desktop.open_sessions). Looking for an
        # emdash task found none, called it "already gone", and the next report
        # named it open again. desktop.close stops its turn, marks it closed (no
        # longer reported or reused) and removes a worktree that holds no work.
        action = desktop.close(cfg, session_key)
        sessions.request_close_report(session_key)
        logger.info("closed desktop session %s (%s)", session_key, action)
        return action
    target = session_target.resolve(emdash_db, session_key, project)
    if target.reason == session_target.ABSENT:
        sessions.request_close_report(session_key)
        logger.info("close %s: no live task by that name under %r — already gone",
                    session_key, project or "any project")
        return "absent"
    if not target.ok:
        if session_key not in _warned:
            _warned.add(session_key)
            logger.warning("close %s REFUSED (%s): cannot tell which project's task it is, "
                           "and a delete by name alone could remove another agent's "
                           "same-named session", session_key, target.reason)
        raise CloseRefused(f"{session_key}: {target.reason}", reason=target.reason)
    result = cdp_control.close_task(session_key, port=cdp_port, project=target.project)
    action = str(result.get("action") or "deleted")
    if action == "absent" and target.project in (emdash.task_projects(emdash_db or "", session_key) or []):
        # The sidebar could not find a row emdash's own DB still holds (a collapsed
        # project hid it, 2026-10-12). That is a close that did not happen, not one
        # that already had: reporting it as gone archives the row for ~10s until the
        # next report revives it, and tells the human it closed.
        raise CloseFailed(f"{session_key}: emdash still has this task under {target.project!r} "
                          "but its sidebar did not show it")
    sessions.request_close_report(session_key)
    logger.info("closed emdash task %s (%s)", session_key, action)
    return action
