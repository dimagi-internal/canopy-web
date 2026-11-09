"""Where meetings live, in the same shape Superwhisper's command-line tool prints, so a
program that reads Superwhisper meetings reads these too.

  ~/.canopy-listen/meetings/<id>/meeting.json   segments, speakers, state
  ~/.canopy-listen/meetings/<id>/audio.wav      the recording (16 kHz mono)
  ~/.canopy-listen/active                       the id being recorded now, if any

meeting.json is replaced whole (written aside, then renamed), so a reader never sees half
of one.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("CANOPY_LISTEN_HOME") or Path.home() / ".canopy-listen")


def meetings_dir() -> Path:
    return home() / "meetings"


def meeting_dir(meeting_id: str) -> Path:
    if not meeting_id or "/" in meeting_id or meeting_id.startswith("."):
        raise ValueError("not a meeting id")
    return meetings_dir() / meeting_id


def new_meeting(title: str = "", source: str = "microphone") -> dict:
    meeting_id = uuid.uuid4().hex
    now = dt.datetime.now(dt.UTC)
    meeting = {
        "id": meeting_id,
        # Superwhisper's format: UTC, no zone suffix.
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}",
        "title": title,
        "source": source,
        "state": "starting",
        "error": "",
        "heard_seconds": 0.0,
        "segments": [],
        "speakers": [],
        "summary": "",
        "transcript": "",
    }
    meeting_dir(meeting_id).mkdir(parents=True)
    save(meeting)
    return meeting


def save(meeting: dict) -> None:
    path = meeting_dir(meeting["id"]) / "meeting.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meeting))
    os.replace(tmp, path)


def load(meeting_id: str) -> dict | None:
    try:
        return json.loads((meeting_dir(meeting_id) / "meeting.json").read_text())
    except (OSError, ValueError):
        return None


def listing(limit: int = 20) -> list[dict]:
    found = []
    root = meetings_dir()
    if root.exists():
        for d in root.iterdir():
            m = load(d.name) if d.is_dir() else None
            if m:
                found.append(m)
    found.sort(key=lambda m: m["datetime"], reverse=True)
    return found[:limit]


def summary_row(m: dict) -> dict:
    """One meeting as `meetings` lists it: no words."""
    return {"id": m["id"], "datetime": m["datetime"], "title": m.get("title", ""), "state": m.get("state", ""),
            "durationMs": int(m.get("heard_seconds", 0) * 1000), "segmentCount": len(m.get("segments", []))}


def set_active(meeting_id: str | None) -> None:
    path = home() / "active"
    if meeting_id:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(meeting_id)
    else:
        path.unlink(missing_ok=True)


def active() -> str | None:
    try:
        return (home() / "active").read_text().strip() or None
    except OSError:
        return None


def delete(meeting_id: str) -> bool:
    d = meeting_dir(meeting_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
