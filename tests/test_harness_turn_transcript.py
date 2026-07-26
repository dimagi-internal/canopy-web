"""Task 1 (run-convergence, canopy side) — the retained raw-transcript store.

canopy's TurnEvent ledger is a deliberately reduced live stream. cost/structure
derivation needs the RAW `claude -p` JSONL, so canopy additionally retains it
per turn, gzipped, off the hot Turn row (TurnTranscript is a sibling, like
TurnEvent). canopy must not parse or re-encode the JSONL — store bytes only.

See .superpowers/sdd/2026-07-26-run-convergence-canopy-side/task-1-brief.md.
"""
from __future__ import annotations

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
    import gzip

    turn = _turn()
    services.append_transcript(turn, ["hello"])

    transcript = TurnTranscript.objects.get(turn=turn)
    # Stored bytes decompress to the raw content; they are not the raw bytes
    # themselves (i.e. they are actually gzip-compressed).
    assert gzip.decompress(bytes(transcript.raw_jsonl_gz)) == b"hello"
