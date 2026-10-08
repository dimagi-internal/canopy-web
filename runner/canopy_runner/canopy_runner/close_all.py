"""Closing every open session on this box — or every one of one project's.

The menu-bar app's "Close sessions" submenu runs `canopy-runner close-sessions`,
a SEPARATE process from the daemon. Each close goes through the same
`close.close_session` the phone's close uses, aimed by (project, task) — never
by name alone, since task names are only unique per project.

Because this is not the daemon, the closing signal `close_session` queues would
die with the process. So once the closes are done this process sends one session
report itself, carrying the deleted names on its `archived:` list; otherwise the
rows would wait out the server's 3-minute liveness window before reading closed.
"""
from __future__ import annotations

import logging
from collections import Counter

from . import close, desktop, emdash, sessions
from .client import Client
from .config import Config

logger = logging.getLogger(__name__)

#: The report's own cap is for a phone list; closing everything must see everything.
ALL_SESSIONS_LIMIT = 10_000


def open_sessions(cfg: Config) -> list[dict]:
    """Every open session on this box: emdash tasks, then Claude desktop sessions.

    Raises emdash.EmdashReadError when emdash could not be read — "I could not
    look" must not read as "there is nothing to close"."""
    rows = emdash.list_open_sessions(cfg.emdash_db, ALL_SESSIONS_LIMIT)
    try:
        rows = rows + desktop.open_sessions(cfg)
    except Exception:  # noqa: BLE001 — a bad desktop index must not hide emdash's
        logger.warning("desktop session read failed; listing emdash sessions only",
                       exc_info=True)
    return rows


def counts_by_project(rows: list[dict]) -> dict[str, int]:
    """{project: open sessions}, most sessions first, then by name."""
    counts = Counter(r.get("project") or "" for r in rows)
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def close_all(cfg: Config, *, project: str | None = None,
              rows: list[dict] | None = None) -> dict:
    """Close every open session, or only `project`'s.

    A session whose project is unknown is skipped rather than closed by name: a
    delete is irreversible and the name alone may be another agent's session.
    Returns {"closed": [...], "absent": [...], "failed": [{task, project, error}],
    "skipped": [...]} — one failure never stops the rest."""
    if rows is None:
        rows = open_sessions(cfg)
    result: dict = {"closed": [], "absent": [], "failed": [], "skipped": []}
    for r in rows:
        task, proj = r.get("emdash_task") or "", r.get("project") or ""
        if not task or (project is not None and proj != project):
            continue
        if not proj:
            result["skipped"].append(task)
            continue
        try:
            action = close.close_session(task, project=proj, cdp_port=cfg.cdp_port,
                                         emdash_db=cfg.emdash_db, cfg=cfg)
        except Exception as exc:  # noqa: BLE001 — CloseRefused, CDPError, anything
            logger.warning("close %s/%s failed: %s", proj, task, exc)
            result["failed"].append({"task": task, "project": proj, "error": str(exc)})
            continue
        result["absent" if action == "absent" else "closed"].append(task)
    return result


def report_closes(cfg: Config, client: Client) -> None:
    """Send the closing signal now (see the module docstring). Best-effort: the
    server reads the tasks' absence as closed within minutes regardless."""
    sessions.request_report_now()
    sessions.maybe_report_sessions(cfg, client)
