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

from . import cdp_control, sessions

logger = logging.getLogger(__name__)


def close_session(session_key: str, *, cdp_port: int = 9222) -> str:
    """Delete `session_key`'s emdash task and queue its closing signal.

    Returns the CDP action — "deleted", or "absent" when the task was already gone
    (a double-tap, or a human who deleted it in emdash a moment earlier). Both
    queue the signal: the task is gone either way, and the server may not know.

    Raises CDPError if the delete could not be completed. The caller logs it and
    moves on; nothing needs undoing.
    """
    result = cdp_control.close_task(session_key, port=cdp_port)
    action = str(result.get("action") or "deleted")
    sessions.request_close_report(session_key)
    logger.info("closed emdash task %s (%s)", session_key, action)
    return action
