"""This laptop's part in moving a session's WORK between boxes (`handoff.py` is
the packing; `apps/canopy_sessions/handoff.py` is the server's flow).

As the SOURCE (`drain`, on the poll tick's report sweep — above the pause gate,
so a box parked because its Claude account ran out of tokens still hands its
work over): pack the session's worktree and upload it, then, once canopy-web says
the target restored it, close the task — through the same close path the web's
Close uses, with its failure reported, never left in this box's log.

As the TARGET (`restore_into`, from the turn executors): unpack the snapshot into
the session's new worktree, leave the report where the agent's first command
reads it, and tell canopy-web.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import handoff
from .client import ClientError

logger = logging.getLogger("canopy_runner")


def source_worktree(cfg, session_key: str, project: str) -> Path | None:
    """Where a session this box runs keeps its work: a Claude CLI / desktop
    session's own worktree, else the emdash task's."""
    from . import desktop, emdash

    wt = desktop.worktree_for(cfg, session_key)
    if wt is not None:
        return wt
    path = emdash.task_worktree(cfg.emdash_db, project, session_key)
    return Path(path) if path else None


def _source_transcript(cfg, session_key: str, project: str) -> Path | None:
    from . import transcript

    home = Path.home()
    return transcript.resolve_transcript(project, session_key, home=home,
                                         claude_home=home / ".claude" / "projects",
                                         emdash_db=cfg.emdash_db)


def drain(cfg, client) -> None:
    """Pack what canopy-web asked for, and close what it says may go."""
    try:
        due = client.sync_handoffs(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("handoff sync failed (non-fatal)", exc_info=True)
        return
    for item in due:
        try:
            if item.get("capture"):
                capture(cfg, client, item)
            elif item.get("close"):
                close(cfg, client, item)
        except Exception:  # noqa: BLE001 — one session must not stop the rest
            logger.warning("handoff %s failed; retried next tick", item.get("id"), exc_info=True)


def capture(cfg, client, item: dict) -> None:
    hid, key, project = item["id"], item.get("session_key") or "", item.get("project") or ""
    wt = source_worktree(cfg, key, project)
    if wt is None or not wt.is_dir():
        client.post_handoff_captured(cfg.runner_id, hid, ok=False,
                                     error=f"no worktree for {key!r} on this box")
        return
    transcript = _source_transcript(cfg, key, project) if item.get("transcript") else None
    try:
        archive, manifest = handoff.capture(wt, transcript=transcript)
    except handoff.SnapshotError as exc:
        logger.warning("handoff %s: could not pack %s: %s", hid, wt, exc)
        client.post_handoff_captured(cfg.runner_id, hid, ok=False, error=str(exc))
        return
    client.upload_handoff(cfg.runner_id, hid, archive, handoff.chunks(archive))
    client.post_handoff_captured(cfg.runner_id, hid, ok=True, manifest=manifest)
    logger.info("handoff %s: packed %s (%s, %d bytes)", hid, key, manifest.get("branch")
                or "detached", len(archive))


def close(cfg, client, item: dict) -> None:
    """Close the task the session left behind. A failure is reported with its
    reason (canopy-web raises an alert) and retried next tick; a reason already
    reported is not sent again."""
    from . import close as close_mod

    hid, key, project = item["id"], item.get("session_key") or "", item.get("project") or ""
    if close_mod.settled_or_in_flight(key):
        return
    try:
        close_mod.close_session(key, project=project, cdp_port=cfg.cdp_port,
                                emdash_db=cfg.emdash_db, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        reason = close_mod.failure_reason(exc, cdp_port=cfg.cdp_port)
        logger.warning("handoff %s: closing %s failed: %s", hid, key, reason)
        if reason != (item.get("close_error") or ""):
            client.post_handoff_close(cfg.runner_id, hid, ok=False, error=reason)
        return
    client.post_handoff_close(cfg.runner_id, hid, ok=True)
    logger.info("handoff %s: closed %s — its work lives on the target now", hid, key)


def handoff_of(turn: dict) -> dict:
    return (turn.get("origin_ref") or {}).get("handoff") or {}


def restore_into(cfg, client, turn: dict, worktree) -> dict | None:
    """If `turn` carries a snapshot, unpack it into `worktree`. Returns the report
    (None when the turn carries none). Never raises: whatever happened is in the
    report, which the agent reads first and canopy-web records."""
    hid = handoff_of(turn).get("id")
    if not hid:
        return None
    if worktree is None:
        report = {"ok": False, "restored": False, "notes": [], "mismatch": [],
                  "error": "the runner could not find this session's worktree"}
    else:
        try:
            archive = client.download_handoff(cfg.runner_id, hid)
            report = handoff.restore(Path(worktree), archive)
        except ClientError as exc:
            report = {"ok": False, "restored": False, "notes": [], "mismatch": [],
                      "error": f"could not download the snapshot: {exc}"}
        handoff.write_marker(Path(worktree), report)
    logger.info("handoff %s: %s", hid, handoff.summary(report))
    try:
        client.post_handoff_restored(cfg.runner_id, hid, report)
    except Exception:  # noqa: BLE001 — the agent still reads the marker
        logger.warning("handoff %s: could not report the restore", hid, exc_info=True)
    return report


def restore_into_task(cfg, client, turn: dict, project: str, task: str, *,
                      timeout: float = 30.0) -> dict | None:
    """`restore_into` for an emdash task, once emdash has recorded its worktree."""
    import os
    import time

    from . import emdash

    if not handoff_of(turn).get("id"):
        return None
    deadline = time.monotonic() + timeout
    path = emdash.task_worktree(cfg.emdash_db, project, task)
    while path is None and os.path.exists(cfg.emdash_db) and time.monotonic() < deadline:
        time.sleep(0.5)
        path = emdash.task_worktree(cfg.emdash_db, project, task)
    return restore_into(cfg, client, turn, Path(path) if path else None)
