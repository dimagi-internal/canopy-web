"""Task 1 (run-convergence, canopy side) — the retained raw-transcript store.

canopy's TurnEvent ledger is a deliberately reduced live stream. cost/structure
derivation needs the RAW `claude -p` JSONL, so canopy additionally retains it
per turn, gzipped, off the hot Turn row (TurnTranscript is a sibling, like
TurnEvent). canopy must not parse or re-encode the JSONL — store bytes only.

See .superpowers/sdd/2026-07-26-run-convergence-canopy-side/task-1-brief.md.
"""
from __future__ import annotations

import gzip
import json
import logging
import zlib

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


def _count_gzip_members(blob: bytes) -> int:
    """How many concatenated gzip members `blob` contains.

    Time-invariant (unlike comparing to a freshly `gzip.compress()`-ed
    blob — that embeds a wall-clock MTIME in its header, which makes exact
    byte-equality assertions a real, if rare, flake). This instead peels off
    one member at a time via a raw gzip-mode `zlib.decompressobj` and counts
    how many times `.unused_data` still has bytes left over.
    """
    count = 0
    data = blob
    while data:
        count += 1
        d = zlib.decompressobj(zlib.MAX_WBITS | 16)
        d.decompress(data)
        d.flush()
        data = d.unused_data
    return count


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
    # Mechanism, time-invariant: assert on gzip STRUCTURE (member count)
    # rather than exact bytes against a freshly computed gzip.compress(...) —
    # that embeds a wall-clock MTIME in its header, so two calls that straddle
    # a second boundary produce different header bytes and a byte-equality
    # assertion would flake for no real reason. Two appends must produce
    # exactly two concatenated members (one per batch); a decompress-all +
    # recompress-all implementation would instead collapse everything into a
    # single member, so this still discriminates the O(1) path from the O(n²)
    # one it replaced.
    assert _count_gzip_members(stored) == 2


def test_append_onto_a_legacy_single_member_row_still_round_trips():
    """Backward-compat regression guard: a row written the OLD way (a single
    gzip member holding the WHOLE accumulated content, as the pre-fix
    decompress-all + recompress-all implementation produced — and as every
    row on this table happened to be, before O(1) appends shipped) must still
    accumulate correctly once a NEW-style append lands on top of it.
    `gzip.decompress` reassembles a legacy single member followed by a new
    member exactly as it would two new-style members — this pins that the
    join/lock logic doesn't secretly depend on the blob always being built
    from same-shaped members.
    """
    turn = _turn()
    legacy_content = b"foo\nbar"
    TurnTranscript.objects.create(
        turn=turn,
        raw_jsonl_gz=gzip.compress(legacy_content),
        line_count=2,
        bytes_raw=len(legacy_content),
    )

    transcript = services.append_transcript(turn, ["baz"])

    assert services.read_transcript(turn) == b"foo\nbar\nbaz"
    assert transcript.line_count == 3
    assert transcript.bytes_raw == len(b"foo\nbar\nbaz")


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


# --- Security review 2026-07-26 fix round -----------------------------------
#
# F2 — a per-turn ceiling (services.TRANSCRIPT_TURN_MAX_BYTES), checked inside
# the same locked transaction as every other append, so a runaway/very-long
# turn can never grow this row past Postgres's bytea limit (or the
# PositiveIntegerField counters) and blow up mid-turn. The real ceiling is
# 100MB; these tests monkeypatch it down to something tiny so they run fast
# and deterministically.


def test_batch_under_the_ceiling_is_unaffected(monkeypatch):
    monkeypatch.setattr(services, "TRANSCRIPT_TURN_MAX_BYTES", 1000)
    turn = _turn()

    transcript = services.append_transcript(turn, ["short line"])

    assert transcript.truncated is False
    assert services.read_transcript(turn) == b"short line"


def test_crossing_the_ceiling_drops_the_batch_and_writes_one_marker(monkeypatch):
    monkeypatch.setattr(services, "TRANSCRIPT_TURN_MAX_BYTES", 10)
    turn = _turn()

    transcript = services.append_transcript(turn, ["this line is way over ten bytes"])

    assert transcript.truncated is True
    raw = services.read_transcript(turn)
    # The real batch content is NOT present — it was dropped, not partially
    # kept — only the synthetic marker line was written.
    assert b"way over ten bytes" not in raw
    marker = json.loads(raw)
    assert marker["type"] == "canopy_transcript_truncated"


def test_after_truncation_every_further_batch_is_a_silent_noop(monkeypatch):
    monkeypatch.setattr(services, "TRANSCRIPT_TURN_MAX_BYTES", 10)
    turn = _turn()
    services.append_transcript(turn, ["over the tiny ceiling"])
    raw_after_marker = services.read_transcript(turn)

    transcript = services.append_transcript(turn, ["more content, still dropped"])

    assert transcript.truncated is True
    # No second marker, no new content — byte-for-byte unchanged.
    assert services.read_transcript(turn) == raw_after_marker


def test_truncation_never_raises_a_running_turn_must_not_4xx(monkeypatch):
    """The whole point of F2: a turn whose transcript got long is still a
    turn that's succeeding. append_transcript must return normally, never
    raise, when it crosses the ceiling."""
    monkeypatch.setattr(services, "TRANSCRIPT_TURN_MAX_BYTES", 1)
    turn = _turn()

    transcript = services.append_transcript(turn, ["anything at all"])

    assert isinstance(transcript, TurnTranscript)


# F5 — a single-slot idempotency guard: a batch_id matching the turn's most
# recently applied batch is recognized as a lost-response retry and dropped,
# not double-appended (this store exists to derive cost, so silent
# duplication would inflate it invisibly).


def test_replaying_the_same_batch_id_is_a_noop():
    turn = _turn()
    services.append_transcript(turn, ["line one"], batch_id="b1")

    replay = services.append_transcript(turn, ["line one"], batch_id="b1")

    assert services.read_transcript(turn) == b"line one"  # not doubled
    assert replay.line_count == 1


def test_a_new_batch_id_after_a_prior_one_still_appends():
    turn = _turn()
    services.append_transcript(turn, ["line one"], batch_id="b1")

    transcript = services.append_transcript(turn, ["line two"], batch_id="b2")

    assert services.read_transcript(turn) == b"line one\nline two"
    assert transcript.line_count == 2


def test_omitting_batch_id_never_dedups():
    """Backward compatible: an older/simpler caller that never sends batch_id
    keeps accumulating normally — dedup is opt-in, not assumed."""
    turn = _turn()
    services.append_transcript(turn, ["line one"])

    transcript = services.append_transcript(turn, ["line one"])

    assert services.read_transcript(turn) == b"line one\nline one"
    assert transcript.line_count == 2


# F3 (revised) — the HTTP GET route streams `iter_transcript`, which inflates
# the stored gzip INCREMENTALLY in bounded chunks, rather than either (a)
# decompressing the whole blob at once (read_transcript's cost) or (b) an
# earlier, since-abandoned approach of serving the still-gzipped bytes with
# Content-Encoding: gzip — a follow-up review found that silently truncates
# on curl --compressed / httpx (both return only the first gzip member of a
# multi-member stream) and our own runner client does no decoding at all.
# iter_transcript yields plain decompressed bytes; only the READ pattern
# (chunked, not all-at-once) differs from read_transcript.


def test_iter_transcript_concatenates_to_the_same_bytes_as_read_transcript():
    turn = _turn()
    services.append_transcript(turn, ["a", "b"])

    chunks = list(services.iter_transcript(turn))

    assert b"".join(chunks) == services.read_transcript(turn) == b"a\nb"


def test_iter_transcript_with_no_rows_yields_nothing():
    turn = _turn()
    assert list(services.iter_transcript(turn)) == []


def test_iter_transcript_respects_a_small_chunk_size_and_still_round_trips():
    """Pin the CHUNKING, not just the outcome: a chunk_size smaller than the
    content must still produce more than one chunk, and concatenating them
    must reproduce the exact original bytes — proves this doesn't secretly
    decompress everything and split it after the fact."""
    turn = _turn()
    content = "x" * 100
    services.append_transcript(turn, [content])

    chunks = list(services.iter_transcript(turn, chunk_size=10))

    assert len(chunks) > 1
    assert all(len(c) <= 10 for c in chunks)
    assert b"".join(chunks) == content.encode("utf-8")


def test_iter_transcript_handles_the_legacy_single_member_format_too():
    """gzip.GzipFile reassembles a concatenated multi-member blob exactly as
    gzip.decompress does — this pins that the CHUNKED reader has the same
    guarantee for a legacy single-member row (see
    test_append_onto_a_legacy_single_member_row_still_round_trips)."""
    turn = _turn()
    legacy_content = b"foo\nbar"
    TurnTranscript.objects.create(
        turn=turn,
        raw_jsonl_gz=gzip.compress(legacy_content),
        line_count=2,
        bytes_raw=len(legacy_content),
    )

    assert b"".join(services.iter_transcript(turn)) == legacy_content
