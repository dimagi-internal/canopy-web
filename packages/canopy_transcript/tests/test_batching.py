"""Byte-bounded batching of transcript payloads.

`chunk_rows` exists because the session stream + backfill had NO bound and went
in one request against Django's DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB) — raised as
RequestDataTooBig BEFORE the view runs, so an unhandled 500, no partial write,
and a runner that retries forever. Measured over 193 local transcripts
(2026-07-31) one full-history payload is already 2.57 MB.
"""
import json

from canopy_transcript import TRANSCRIPT_BATCH_MAX_BYTES, chunk_rows


def test_splits_on_the_byte_budget_and_never_drops_or_reorders_a_row():
    rows = [{"index": i, "role": "assistant", "text": "x" * 100} for i in range(50)]
    batches = chunk_rows(rows, max_bytes=1000)
    assert len(batches) > 1
    assert [r for b in batches for r in b] == rows
    for b in batches[:-1]:
        assert len(json.dumps(b).encode()) <= 1000 + 200   # budget + one row's slack


def test_emits_an_oversized_row_alone_rather_than_dropping_it():
    """Unlike chunk_raw_lines, which substitutes a marker. A row is already capped
    at the producer (TOOL_TEXT_MAX 8 KB), so this cannot realistically happen — and
    a raw line has a marker convention that keeps the gap visible, where a dropped
    transcript row would just silently lose a message."""
    big = {"index": 0, "role": "tool_result", "text": "x" * 5000}
    assert chunk_rows([big], max_bytes=100) == [[big]]


def test_empty_input_yields_no_batches():
    """Callers add their own `or [[]]` when an empty POST is still meaningful (the
    backfill's final chunk retires the request), so this must not invent one."""
    assert chunk_rows([]) == []


def test_default_budget_stays_under_the_servers_request_ceiling():
    rows = [{"index": i, "text": "y" * 4000} for i in range(2000)]
    for b in chunk_rows(rows):
        assert len(json.dumps(b).encode()) < 2_621_440   # DATA_UPLOAD_MAX_MEMORY_SIZE
    assert TRANSCRIPT_BATCH_MAX_BYTES < 2_621_440
