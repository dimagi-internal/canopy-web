"""Read JSONL transcript records, best-effort."""
from __future__ import annotations

import json
from pathlib import Path


def read_records(path) -> list[dict]:
    """Every JSONL record in the transcript, best-effort (never raises)."""
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# How much of a transcript's tail is enough to see how the last turn ended.
# The records that answer that question are the final few, and a live ACE run's
# file is megabytes — reading the whole thing on every `Notification` would put
# a multi-megabyte read on the hot path of a hook that fires every idle minute.
TAIL_BYTES = 65_536


def read_tail_records(path, *, max_bytes: int = TAIL_BYTES) -> list[dict]:
    """The LAST records in a transcript, best-effort (never raises).

    The first line of the window is dropped whenever the file is bigger than the
    window, because a byte offset lands mid-line and a half-record parses as
    nothing at best and as something else at worst.
    """
    try:
        p = Path(path)
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            blob = f.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out
