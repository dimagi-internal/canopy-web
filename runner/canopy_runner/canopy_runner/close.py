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
"""
from __future__ import annotations

import logging

from . import cdp_control, session_target, sessions

logger = logging.getLogger(__name__)


class CloseRefused(Exception):
    """The session could not be placed in exactly one emdash project, so it was
    NOT deleted. A delete is irreversible; by name alone it can take another
    agent's same-named session (ada's and eva's "editing", 2026-10-04)."""


# Keys already warned about, so a refusal retried every poll tick logs once.
_warned: set[str] = set()


def close_session(session_key: str, *, project: str = "", cdp_port: int = 9222,
                  emdash_db: str | None = None, cfg=None) -> str:
    """Delete `session_key`'s emdash task — the one under `project` — and queue its
    closing signal.

    Returns the CDP action — "deleted", or "absent" when the task was already gone
    (a double-tap, or a human who deleted it in emdash a moment earlier). Both
    queue the signal: the task is gone either way, and the server may not know.

    Raises CloseRefused, touching nothing, when the project cannot be resolved
    (see session_target) — never a delete by name alone. Raises CDPError if the
    delete could not be completed. The caller logs either and moves on.

    A Claude desktop session (keyed by its session id) is closed by its own
    runtime. Looking it up in emdash found nothing, read as "already gone": canopy
    was told it closed while it kept running, and the next report reopened it.
    """
    if cfg is not None:
        from . import desktop

        if desktop.is_desktop_session(cfg, session_key):
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
        raise CloseRefused(f"{session_key}: {target.reason}")
    result = cdp_control.close_task(session_key, port=cdp_port, project=target.project)
    action = str(result.get("action") or "deleted")
    sessions.request_close_report(session_key)
    logger.info("closed emdash task %s (%s)", session_key, action)
    return action
