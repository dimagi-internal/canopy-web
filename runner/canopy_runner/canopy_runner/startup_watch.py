"""A session canopy created that never started is waiting on someone (#1190).

**The failure.** Claude Code can stop at a dialog before a session's first turn
starts. Two seen so far: the one-time "Claude in Chrome extension detected" offer,
and the folder-trust gate. Nothing in the existing chain notices. No hook has
fired yet (hooks belong to a running session), so `pending-menus.json` stays
empty. No transcript exists yet either, so `pending_question` has nothing to
read. canopy-web then shows `running=false, waiting_on_you=false`, which reads
as idle. On haldimagi's runner (2026-10-06) a dispatched turn sat on the Chrome
offer for 16 minutes like that. One Enter in emdash unblocked it.

**The signal.** A session canopy CREATED that has no transcript a couple of
minutes later has not started. Claude Code writes the transcript as soon as the
first prompt is accepted, and the chat path already relies on that (it waits up
to 45s for it). Two minutes with no transcript means Claude Code is stuck in
front of the prompt, and in practice that is a startup dialog. The session is
reported with an option-less "waiting on you" marker, the same shape a
`Notification` hook produces (`canopy_transcript.marker_from_hook`): words and no
buttons, pointing the human at emdash. canopy-web already renders that shape and
counts it as waiting on you. It does not lock the composer (`menuBlocksComposer`).

**What the marker does NOT do.** It does not say which dialog it is. Reading the
screen needs CDP, which clicks the task and steals focus (#510). Guessing from
timing is how a signal starts lying. It does not answer the dialog either. A
startup dialog's default is not always the safe choice: the trust gate's default
is "Yes, I trust this folder", and the Chrome offer's answer is saved as a
machine-wide preference. That stays a human's decision.

**Why only sessions canopy created.** An emdash task a human opened and has not
typed into also has no transcript, and that is not a stall. So only the names
recorded at CREATE are watched. A watched name leaves the watch once its
transcript appears, once its task leaves the open set, or after `MAX_AGE_S`.

**Why it survives a restart, unlike a notification marker.** The runner restarts
itself on every self-update. A notification marker is not restored after a
restart (see menu_store) because it claims a turn was in flight, and nothing can
re-check that claim. This one is re-derived on every report from a fact on disk
("no transcript yet"), so restoring the creation time cannot make it lie. It
fails toward silence: an unreadable store is empty, which is the old behaviour.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

#: How long a created session may go without a transcript before it counts as
#: stuck. A normal start writes the transcript within seconds; the chat path
#: gives up waiting after 45s. Two minutes leaves room for a slow box without
#: leaving a dispatched turn invisible for long.
STALL_AFTER_S = 120

#: When a watched name is dropped even if it never started. By then a human has
#: either answered the dialog or killed the task. A transcript the resolver can
#: never find must not keep a session "waiting on you" forever.
MAX_AGE_S = 24 * 60 * 60

SOURCE = "startup"

_lock = threading.Lock()
#: {(project, task): unix time canopy created it}
_created: dict[tuple[str, str], float] = {}
_path: Path | None = None
_loaded = False


def configure(path) -> None:
    """Where the watch persists. Read once per process, on first use."""
    global _path, _loaded
    with _lock:
        _path = Path(path) if path else None
        _loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    if _path is None:
        return
    try:
        rows = json.loads(_path.read_text())
    except FileNotFoundError:
        return
    except Exception:  # noqa: BLE001 — a corrupt file is an empty one
        logger.debug("startup watch unreadable at %s", _path, exc_info=True)
        return
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        project, task, ts = row.get("project"), row.get("task"), row.get("created_at")
        if (isinstance(project, str) and isinstance(task, str) and task
                and isinstance(ts, (int, float))):
            _created.setdefault((project, task), float(ts))


def _save() -> None:
    if _path is None:
        return
    rows = [{"project": p, "task": t, "created_at": ts} for (p, t), ts in _created.items()]
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _path.with_suffix(_path.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, indent=2))
        tmp.replace(_path)
    except OSError:
        logger.debug("startup watch not saved to %s", _path, exc_info=True)


def note_created(project: str, task: str, *, now=None) -> None:
    """Record that canopy just created emdash task `task` under `project`."""
    if not task:
        return
    with _lock:
        _ensure_loaded()
        _created[(project or "", task)] = float(now() if now else time.time())
        _save()


def _forget(keys) -> None:
    for key in keys:
        _created.pop(key, None)


def attach_stall_markers(sessions: list[dict], *, has_transcript, now=None) -> None:
    """Set `question` to a startup-stall marker on each reported session canopy
    created that has not started, in place. Best-effort; never raises.

    `has_transcript(project, task) -> bool` is the resolver. It is only called for
    watched sessions, so the cost is bounded by how many sessions canopy created
    recently, not by the size of the report.

    A session that already carries a question is left alone: a real menu or a
    hook marker knows more than this does. The report is also where the watch is
    pruned, because the report is what sees the whole open set.
    """
    try:
        with _lock:
            _ensure_loaded()
            if not _created:
                return
            t = float(now() if now else time.time())
            open_tasks = {s.get("emdash_task") or "" for s in sessions}
            done = {k for k, ts in _created.items()
                    if k[1] not in open_tasks or t - ts >= MAX_AGE_S}
            _forget(done)
            for s in sessions:
                project, task = s.get("project") or "", s.get("emdash_task") or ""
                key = _match(project, task)
                if key is None:
                    continue
                try:
                    started = has_transcript(project, task)
                except Exception:  # noqa: BLE001 — unknown: say nothing, check next report
                    logger.debug("transcript lookup failed for %s/%s", project, task,
                                 exc_info=True)
                    continue
                if started:
                    _forget([key])  # it started; nothing more to watch
                    done.add(key)
                    continue
                created = _created[key]
                if t - created < STALL_AFTER_S or s.get("question"):
                    continue
                s["question"] = stall_marker(created)
            if done:
                _save()
    except Exception:  # noqa: BLE001 — this runs inside the liveness report
        logger.debug("startup-stall check failed (non-fatal)", exc_info=True)


def _match(project: str, task: str) -> tuple[str, str] | None:
    """The watched key for a reported session, or None.

    Exact (project, task) first. If that misses, a unique match on the task name
    alone: the project canopy drove (an agent slug or a repo name) and the name
    emdash reports for it are normally identical, but nothing guarantees that. A
    name canopy generates carries a thread discriminator, so two watched sessions
    sharing one is not expected. If it happens anyway, matching neither is the
    safe answer."""
    if (project, task) in _created:
        return (project, task)
    hits = [k for k in _created if k[1] == task]
    return hits[0] if len(hits) == 1 else None


def stall_marker(created_at: float) -> dict:
    """An option-less menu in the shape `marker_from_hook` produces.

    Every field is fixed for a given session, including `observed_at` (the first
    moment the stall could be seen). The server compares the whole dict on each
    report and treats any difference as a new menu: it sends a `session.menu`
    frame each time, and a changed ask can trigger a push. A running minute count
    or a fresh timestamp would make every report look like a new ask."""
    return {
        "question": (
            "This session has not started. canopy created it more than "
            f"{STALL_AFTER_S // 60} minutes ago and Claude Code has not written "
            "anything yet. That usually means a startup "
            "dialog is waiting for an answer (for example the Claude in Chrome offer "
            "or a folder-trust prompt). Open the session in emdash to answer it."
        ),
        "title": "Waiting on you",
        "body": "",
        "selected": None,
        "options": [],
        "source": SOURCE,
        "observed_at": created_at + STALL_AFTER_S,
    }


def _reset_for_tests() -> None:
    global _path, _loaded
    with _lock:
        _created.clear()
        _path = None
        _loaded = False
