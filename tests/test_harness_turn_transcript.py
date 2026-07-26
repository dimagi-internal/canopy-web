"""Task 1 (run-convergence, canopy side) — the retained raw-transcript store.

canopy's TurnEvent ledger is a deliberately reduced live stream. cost/structure
derivation needs the RAW `claude -p` JSONL, so canopy additionally retains it
per turn, gzipped, off the hot Turn row (TurnTranscript is a sibling, like
TurnEvent). canopy must not parse or re-encode the JSONL — store bytes only.

See .superpowers/sdd/2026-07-26-run-convergence-canopy-side/task-1-brief.md.
"""
from __future__ import annotations

import gzip
import logging

import pytest

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn, TurnTranscript

pytestmark = pytest.mark.django_db


def _turn(idempotency_key: str = "k1") -> Turn:
    agent = Agent.objects.create(slug="echo", name="Echo")
    return Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_BOARD, idempotency_key=idempotency_key
    )


def test_append_transcript_is_retrievable():
    turn = _turn()
    lines = ['{"type": "assistant", "text": "hi"}', '{"type": "result"}']

    services.append_transcript(turn, lines)

    raw = services.read_transcript(turn)
    assert raw == "\n".join(lines).encode("utf-8")


def test_append_transcript_twice_accumulates_rather_than_replaces():
    turn = _turn()
    first = ['{"a": 1}']
    second = ['{"a": 2}']

    services.append_transcript(turn, first)
    services.append_transcript(turn, second)

    raw = services.read_transcript(turn)
    assert raw == b'{"a": 1}\n{"a": 2}'


def test_append_transcript_counters_reflect_accumulation():
    turn = _turn()
    first = ["a" * 10]
    second = ["b" * 5, "c" * 3]

    services.append_transcript(turn, first)
    transcript = services.append_transcript(turn, second)

    assert transcript.line_count == 3
    # 10 + "\n" + 5 + "\n" + 3 == 20 raw bytes.
    assert transcript.bytes_raw == 20


def test_append_transcript_returns_the_same_row_each_call():
    turn = _turn()
    services.append_transcript(turn, ["one"])
    services.append_transcript(turn, ["two"])

    assert TurnTranscript.objects.filter(turn=turn).count() == 1


def test_read_transcript_with_no_rows_returns_empty_bytes_not_raises():
    turn = _turn()
    assert services.read_transcript(turn) == b""


def test_exact_byte_round_trip_unicode_and_literal_backslash_n():
    turn = _turn()
    lines = [
        '{"text": "héllo wörld 🎉"}',
        '{"text": "line with a literal \\\\n escape, not a real newline"}',
    ]

    services.append_transcript(turn, lines)

    raw = services.read_transcript(turn)
    assert raw == "\n".join(lines).encode("utf-8")
    # Decoded back, the literal backslash-n survives as two characters, not a newline.
    decoded = raw.decode("utf-8")
    assert "\\n escape" in decoded
    assert decoded.count("\n") == 1  # only the join separator, not the escape


def test_append_transcript_returns_a_turn_transcript_instance():
    turn = _turn()
    result = services.append_transcript(turn, ["x"])
    assert isinstance(result, TurnTranscript)
    assert result.turn_id == turn.pk


def test_transcript_is_gzipped_on_disk():
    turn = _turn()
    services.append_transcript(turn, ["hello"])

    transcript = TurnTranscript.objects.get(turn=turn)
    # Stored bytes decompress to the raw content; they are not the raw bytes
    # themselves (i.e. they are actually gzip-compressed).
    assert gzip.decompress(bytes(transcript.raw_jsonl_gz)) == b"hello"


# --- Fix round: review findings -------------------------------------------
#
# IMPORTANT 1 — a singleton blank-line batch must never desync line_count
# from the stored bytes. `"\n".join([""])` is `""`, so a naive implementation
# counts a line while storing zero bytes. Task 2's caller splits a stream
# chunk on "\n" and will periodically hand append_transcript exactly this
# batch (the trailing empty segment).


def test_blank_batch_as_first_append_is_a_true_noop():
    turn = _turn()

    transcript = services.append_transcript(turn, [""])

    assert transcript.line_count == 0
    assert transcript.bytes_raw == 0
    assert services.read_transcript(turn) == b""


def test_blank_batch_on_top_of_existing_content_does_not_change_counters():
    turn = _turn()
    services.append_transcript(turn, ["real line"])

    transcript = services.append_transcript(turn, [""])

    assert transcript.line_count == 1
    assert transcript.bytes_raw == len(b"real line")
    assert services.read_transcript(turn) == b"real line"


def test_blank_element_among_real_lines_is_dropped_not_counted():
    turn = _turn()

    transcript = services.append_transcript(turn, ["a", "", "b"])

    assert transcript.line_count == 2
    assert services.read_transcript(turn) == b"a\nb"


# IMPORTANT 2 — appends must be O(1): a new gzip member per batch, never
# decompress-all + recompress-all. gzip.decompress transparently reassembles
# a concatenated multi-member stream, so this is backward compatible with a
# row already written as a single member (no migration).


def test_many_small_batches_round_trip_exactly():
    turn = _turn()
    expected_lines = [f"line-{i}" for i in range(50)]

    for line in expected_lines:
        services.append_transcript(turn, [line])

    transcript = TurnTranscript.objects.get(turn=turn)
    expected = "\n".join(expected_lines).encode("utf-8")
    assert services.read_transcript(turn) == expected
    assert transcript.line_count == 50
    assert transcript.bytes_raw == len(expected)


def test_appends_concatenate_gzip_members_rather_than_recompress_whole_blob():
    turn = _turn()
    services.append_transcript(turn, ["hello"])
    services.append_transcript(turn, ["world"])

    transcript = TurnTranscript.objects.get(turn=turn)
    stored = bytes(transcript.raw_jsonl_gz)

    # Correctness: still decompresses to the full accumulated content.
    assert gzip.decompress(stored) == b"hello\nworld"
    # Mechanism: the stored blob is the concatenation of two INDEPENDENTLY
    # compressed members, not a single fresh compression of the combined
    # content — proves the O(1)-per-append path is actually taken (a
    # decompress-all + recompress-all implementation would produce the
    # single-member form instead).
    assert stored == gzip.compress(b"hello") + gzip.compress(b"\nworld")
    assert stored != gzip.compress(b"hello\nworld")


# MINOR 3 — a line containing an embedded "\n" violates the one-record-per-
# element contract; canopy must surface it (log), not swallow it silently.


def test_embedded_newline_in_a_line_logs_a_warning(caplog):
    # The "apps" logger is configured with propagate=False (config/settings/
    # base.py) so it never reaches root — attach caplog's own handler
    # directly to the emitting logger rather than relying on propagation.
    turn = _turn()
    target_logger = logging.getLogger("apps.harness.services")
    target_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="apps.harness.services"):
            services.append_transcript(turn, ["line one\nline two"])
    finally:
        target_logger.removeHandler(caplog.handler)

    assert any("embedded" in record.getMessage().lower() for record in caplog.records)


def test_line_without_embedded_newline_logs_nothing(caplog):
    turn = _turn()
    target_logger = logging.getLogger("apps.harness.services")
    target_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="apps.harness.services"):
            services.append_transcript(turn, ["a perfectly normal line"])
    finally:
        target_logger.removeHandler(caplog.handler)

    assert len(caplog.records) == 0
