"""Incremental byte-offset reader for an append-only JSONL transcript.

Reads ONLY the newly-appended bytes on each call — O(new data), never re-reading
the session's history — so it stays cheap no matter how long a session runs. This
is what the continuous session tailer uses to stream live emdash activity to the
phone without duplicating (or even re-reading) the durable transcript, which stays
owned by Claude Code / emdash.

Handles the two real-world hazards of tailing a file another process is writing:
- a partial trailing line (the writer is mid-append) is buffered, not parsed, and
  completed on the next read;
- truncation/rotation (the file shrinks below our offset) resets the offset so we
  don't read garbage.
"""
from __future__ import annotations

import json
import os


class TailReader:
    """Stateful incremental reader for one transcript path.

    `read_new()` returns the JSON records appended since the last call. `seek_end()`
    skips the existing history so the first `read_new()` yields only NEW activity
    (the tailer starts here — the durable history is already visible elsewhere)."""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self.offset = 0
        self._partial = b""
        # The RAW text of the lines the most recent read_new() consumed, in the
        # same order. Kept alongside the parsed records because a turn's retained
        # transcript must be the CLI's own bytes — re-encoding a parsed record
        # would change field order and number formatting, and the whole point of
        # storing raw JSONL is that a consumer can re-derive from it exactly.
        # A line that fails to parse is still raw content, so it is kept here
        # even though it produces no record.
        self.last_raw: list[str] = []

    def seek_end(self) -> None:
        try:
            self.offset = os.path.getsize(self.path)
        except OSError:
            self.offset = 0
        self._partial = b""

    def read_new(self) -> list[dict]:
        self.last_raw = []
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.offset:  # truncated / rotated — start over
            self.offset = 0
            self._partial = b""
        if size <= self.offset:
            return []
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return []
        self.offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()  # trailing element = incomplete line (or b"")
        out: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Decoded, never re-encoded: this is the CLI's own byte content, and
            # a retained transcript is only re-derivable if it stays verbatim.
            self.last_raw.append(line.decode("utf-8", errors="replace"))
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
