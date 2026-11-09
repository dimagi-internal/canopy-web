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


def socket_path() -> Path:
    """`canopy-listen serve`'s unix socket. A socket path must be under 104 bytes on macOS, so a
    home that deep (a test's temp folder) gets a short stand-in named after it."""
    path = home() / "serve.sock"
    if len(str(path).encode()) < 100:
        return path
    import hashlib
    import tempfile

    tag = hashlib.sha256(str(home()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"canopy-listen-{tag}.sock"


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


def names(meeting_id: str) -> dict[int, str]:
    """Speaker names the person gave, by number: names.json beside meeting.json. The recorder
    owns meeting.json while it runs, so a rename goes here and the recorder merges it."""
    try:
        raw = json.loads((meeting_dir(meeting_id) / "names.json").read_text())
        return {int(k): str(v) for k, v in raw.items()}  # "" means back to "Speaker N"
    except (OSError, ValueError, AttributeError):
        return {}


def set_name(meeting_id: str, number: int, name: str) -> dict[int, str]:
    current = names(meeting_id)
    name = " ".join(str(name).split())[:60]
    current[int(number)] = name
    path = meeting_dir(meeting_id) / "names.json"
    tmp = path.with_suffix(".tmp-names")
    tmp.write_text(json.dumps({str(k): v for k, v in current.items()}))
    os.replace(tmp, path)
    return current


def delete(meeting_id: str) -> bool:
    d = meeting_dir(meeting_id)
    if not d.exists():
        return False
    shutil.rmtree(d)
    return True
