"""Byte-bounded batching for transcript payloads.

The server 422s a single request whose line bytes exceed 1 MiB
(apps/harness/api.py::TRANSCRIPT_APPEND_MAX_BYTES), so batches are bounded by
BYTES, not line count: one tool-result line can be enormous on its own.

`chunk_rows` applies the same budget to structured transcript rows (the session
stream and backfill payloads). Those had NO bound at all and went in one request
against Django's DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) — which is worse than a
422, because Django raises RequestDataTooBig before the view runs: an unhandled
500, no partial write, and a runner that retries it forever. Measured over 193
local transcripts (2026-07-31), one full-history payload is already 2.57 MB.
"""
from __future__ import annotations

import json

TRANSCRIPT_BATCH_MAX_BYTES = 900 * 1024


def chunk_raw_lines(
    lines: list[str], max_bytes: int = TRANSCRIPT_BATCH_MAX_BYTES
) -> list[list[str]]:
    """Split raw JSONL lines into batches whose UTF-8 size stays under `max_bytes`.

    A single line larger than the cap can never fit, even alone — the server
    rejects the whole request on total bytes. Shipping it anyway would fail every
    retry and stall the flush, so it is replaced by a marker line: a consumer
    re-deriving cost from this transcript then SEES a gap instead of silently
    reading an incomplete turn as a complete one.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        n = len(line.encode("utf-8")) + 1  # +1 for the newline the server joins on
        if n > max_bytes:
            if current:
                batches.append(current)
                current, size = [], 0
            batches.append([json.dumps({"type": "canopy_runner_line_dropped",
                                        "bytes": n})])
            continue
        if size + n > max_bytes and current:
            batches.append(current)
            current, size = [], 0
        current.append(line)
        size += n
    if current:
        batches.append(current)
    return batches


def chunk_rows(
    rows: list[dict], max_bytes: int = TRANSCRIPT_BATCH_MAX_BYTES
) -> list[list[dict]]:
    """Split structured transcript rows into batches under `max_bytes`.

    Unlike `chunk_raw_lines`, an oversized row is emitted ALONE rather than
    replaced by a marker: these rows are already capped at the producer
    (TOOL_TEXT_MAX 8 KB, TOOL_INPUT_STR_MAX 4 KB), so one cannot realistically
    approach the budget — and where a raw line has a marker convention that keeps
    a gap visible, a dropped transcript row would just silently lose a message.
    """
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for row in rows:
        n = len(json.dumps(row, default=str).encode("utf-8")) + 1
        if current and size + n > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(row)
        size += n
    if current:
        batches.append(current)
    return batches
