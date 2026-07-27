"""Byte-bounded batching for a turn's retained raw transcript.

The server 422s a single request whose line bytes exceed 1 MiB
(apps/harness/api.py::TRANSCRIPT_APPEND_MAX_BYTES), so batches are bounded by
BYTES, not line count: one tool-result line can be enormous on its own.
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
