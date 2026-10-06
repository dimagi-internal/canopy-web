"""A turn's record: the append-only `TurnEvent` ledger and the retained raw
transcript (`TurnTranscript`), with the readers over both.

`append_events` is the one writer of the ledger and fires
`turn_events_appended`; `append_transcript` accumulates a turn's raw JSONL under a
per-turn ceiling. Split out of `services.py`, which still re-exports every name
here.
"""
from __future__ import annotations

import gzip
import io
import json
import logging

from django.db import transaction
from django.db.models import Max

from .models import (
    Turn,
    TurnEvent,
    TurnTranscript,
)

logger = logging.getLogger(__name__)


def append_events(turn: Turn, events: list[dict]) -> int:
    with transaction.atomic():
        # Lock the turn row first so concurrent appenders to the same turn
        # serialize on the Max("seq") read instead of racing each other into
        # the turnevent_seq_unique_per_turn index (sqlite ignores
        # select_for_update; Postgres serializes — that's the point).
        Turn.objects.select_for_update().get(pk=turn.pk)
        current = (
            TurnEvent.objects.filter(turn=turn).aggregate(m=Max("seq"))["m"] or 0
        )
        rows = [
            TurnEvent(turn=turn, seq=current + i + 1, kind=e["kind"], payload=e.get("payload", {}))
            for i, e in enumerate(events)
        ]
        TurnEvent.objects.bulk_create(rows)

    # Fire AFTER commit so subscribers (apps/realtime) fan out durable rows and
    # never race the DB. Local import + on_commit avoids an import-time cycle and
    # a fan-out on a transaction that ultimately rolls back. bulk_create emits no
    # post_save, so this signal is the only hook a live tail can ride.
    def _fire_appended():
        from apps.harness.signals import turn_events_appended

        turn_events_appended.send(sender=Turn, turn=turn, rows=rows)

    transaction.on_commit(_fire_appended)
    return len(rows)


# Per-TURN ceiling on retained raw transcript content (security review
# 2026-07-26, F2). `raw_jsonl_gz` is Postgres `bytea` (1GB hard limit) and
# `bytes_raw` a `PositiveIntegerField` (2GB) — an unbounded single turn would
# eventually hit one of those and raise a raw DB error mid-turn with no
# upstream signal. 100MB (uncompressed) is generous headroom above even an
# unusually long, tool-output-heavy turn while sitting multiple orders of
# magnitude under both hard limits, so this is a backstop against a runaway
# turn, not a realistic ceiling for normal use.
TRANSCRIPT_TURN_MAX_BYTES = 100 * 1024 * 1024


def append_transcript(turn: Turn, raw_lines: list[str], *, batch_id: str = "") -> TurnTranscript:
    """Accumulate raw `claude -p` JSONL lines onto a turn's retained transcript.

    Idempotent-per-turn in the sense that repeated calls ACCUMULATE (a turn
    streams in batches over its lifetime) — never replace. Lines are joined
    with a bare "\\n" exactly as the CLI's own JSONL framing does; no
    re-encoding, no reordering, no rewriting a line's content. canopy stores
    bytes only and never parses this JSONL — that stays the consumer's job.

    O(1) per append: rather than decompress-everything-then-recompress-
    everything (O(total accumulated) on every call — expensive while holding
    the same Turn row lock the claim/finish paths take), this gzip-compresses
    only THIS batch and concatenates the resulting gzip member onto the
    stored blob. `gzip.decompress` transparently reassembles a concatenated
    multi-member stream (stdlib-verified:
    `gzip.decompress(gzip.compress(b"a") + gzip.compress(b"b")) == b"ab"`),
    so this is backward compatible with rows already written as a single
    member — no migration, no format break.

    A caller that splits a stream chunk on "\\n" will periodically produce a
    batch whose only element is the trailing empty segment — a real but
    zero-byte "line". Those are dropped before counting/encoding so
    `line_count` never claims a line the stored bytes don't have; an
    all-blank batch is a true no-op (existing content, counters, and stored
    bytes are all left untouched).

    An element that itself contains an embedded "\\n" violates the one-
    JSONL-record-per-element contract (it understates `line_count` and would
    inject a stray join at the next append) — logged as a warning so a
    Task-2 upstream bug surfaces at the boundary instead of as an unexplained
    later cost discrepancy. Not raised: a malformed batch should still be
    retained, not dropped.

    `batch_id` (security review F5) is an optional caller-supplied idempotency
    key for THIS batch. If it matches the turn's `last_batch_id` — the
    immediately preceding call — this is a retry after a lost response, and
    the batch is dropped as a no-op rather than double-appended (a lost-ack
    retry is the realistic case; an arbitrary OLDER batch replayed later is
    not guarded against). Omit it (empty string, the default) to skip
    dedup entirely — existing/older callers are unaffected.

    Per-turn size ceiling (F2): once accumulated `bytes_raw` would cross
    `TRANSCRIPT_TURN_MAX_BYTES`, this batch's actual content is DROPPED and a
    single synthetic marker line is written in its place, then `truncated`
    latches permanently — every later call for this turn is a silent no-op.
    A turn still executing must not be failed over transcript SIZE, so this
    never raises; the caller (the HTTP route) always sees success.
    """
    # Drop truly-empty elements (see docstring) before both the count and the
    # join — a splitter's trailing "" must never count as a stored line.
    lines = [line for line in raw_lines if line != ""]

    if any("\n" in line for line in lines):
        logger.warning(
            "append_transcript(turn=%s): a raw line contains an embedded "
            "newline, violating the one-JSONL-record-per-element contract — "
            "line_count and the stored join structure will be wrong for this "
            "batch",
            turn.pk,
        )

    with transaction.atomic():
        # Lock the turn row first so concurrent appenders to the same turn
        # serialize (mirrors append_events — sqlite ignores select_for_update,
        # Postgres serializes, which is the point).
        Turn.objects.select_for_update().get(pk=turn.pk)
        transcript = (
            TurnTranscript.objects.select_for_update().filter(turn=turn).first()
        )

        if batch_id and transcript is not None and transcript.last_batch_id == batch_id:
            # A retry of the batch we JUST applied (its response was lost in
            # transit) — already reflected in the stored content, so this is
            # a no-op, not a double-append.
            return transcript

        if transcript is not None and transcript.truncated:
            # Per-turn ceiling already hit — drop everything further,
            # including a marker (that was written exactly once, at the
            # crossing call below).
            return transcript

        content = "\n".join(lines)
        added_lines = len(lines)
        existing_bytes = transcript.bytes_raw if transcript is not None else 0
        newly_truncated = False
        if existing_bytes + len(content.encode("utf-8")) > TRANSCRIPT_TURN_MAX_BYTES:
            # This batch would cross the ceiling. Drop its actual content —
            # never mind what it was — and write ONE synthetic marker line
            # instead, so a re-derivation downstream can see the transcript
            # was cut off rather than silently ending mid-stream.
            content = json.dumps({
                "type": "canopy_transcript_truncated",
                "reason": (
                    f"turn transcript exceeded {TRANSCRIPT_TURN_MAX_BYTES} "
                    "bytes; further content for this turn was dropped"
                ),
            })
            added_lines = 1
            newly_truncated = True

        # A bare "\n" glues this content onto whatever's already stored — but
        # only when both sides are non-empty, so a first-ever or all-blank
        # batch never introduces a phantom separator.
        if transcript is not None and transcript.bytes_raw and content:
            new_raw = ("\n" + content).encode("utf-8")
        else:
            new_raw = content.encode("utf-8")

        added_bytes = len(new_raw)
        new_member = gzip.compress(new_raw) if new_raw else b""

        if transcript is None:
            transcript = TurnTranscript.objects.create(
                turn=turn,
                raw_jsonl_gz=new_member,
                line_count=added_lines,
                bytes_raw=added_bytes,
                truncated=newly_truncated,
                last_batch_id=batch_id,
            )
        elif new_member:
            transcript.raw_jsonl_gz = bytes(transcript.raw_jsonl_gz) + new_member
            transcript.line_count = transcript.line_count + added_lines
            transcript.bytes_raw = transcript.bytes_raw + added_bytes
            if newly_truncated:
                transcript.truncated = True
            if batch_id:
                transcript.last_batch_id = batch_id
            transcript.save(
                update_fields=[
                    "raw_jsonl_gz", "line_count", "bytes_raw", "truncated",
                    "last_batch_id", "updated_at",
                ]
            )
        # else: an all-blank batch on top of existing content — nothing new
        # to add, leave the row untouched (batch_id is deliberately not
        # recorded here either: replaying a genuinely blank batch is already
        # a no-op, so there's nothing dedup needs to protect).
        return transcript


def read_transcript(turn: Turn) -> bytes:
    """Decompressed raw JSONL for a turn, or b"" if nothing was ever appended
    (a turn with no transcript is common — e.g. non-CLI turns — and must read
    as empty rather than raise).

    For in-process consumers only (e.g. a future cost-derivation job running
    server-side, or anything that genuinely needs the whole blob at once).
    The HTTP read route does NOT call this — see `iter_transcript`, which
    streams bounded chunks instead of materializing the whole decompressed
    blob in a web worker's memory (security review 2026-07-26, F3)."""
    transcript = TurnTranscript.objects.filter(turn=turn).first()
    if transcript is None or not transcript.raw_jsonl_gz:
        return b""
    return gzip.decompress(bytes(transcript.raw_jsonl_gz))


def iter_transcript(turn: Turn, *, chunk_size: int = 64 * 1024):
    """Yield a turn's DECOMPRESSED raw JSONL in bounded chunks, inflating
    incrementally rather than materializing the whole decompressed blob at
    once (security review 2026-07-26, F3; the sibling `/events` route caps
    at 500 rows for the same underlying reason — nothing about this route
    may scale with transcript size).

    An EARLIER version of this fix instead served the STILL-GZIPPED bytes
    directly with `Content-Encoding: gzip`, betting on the HTTP client to
    inflate transparently. A follow-up review empirically falsified that:
    `curl --compressed` and `httpx` both return only the FIRST gzip member
    of a multi-member stream (Task 1's own on-disk format — see
    `append_transcript`) — a 200 with silently TRUNCATED content, no error,
    exactly the corrupted-derivation failure mode F5's idempotency work
    exists to prevent. Worse, this repo's own runner client
    (`runner/canopy_runner`, `urllib.request`) sends no `Accept-Encoding`
    and does no decoding at all — it would treat raw gzip bytes as JSONL.
    Streaming plaintext removes the wire-format gamble entirely: every
    caller sees the same bytes `read_transcript` would return, with none of
    read_transcript's all-at-once memory cost.

    `gzip.GzipFile` transparently reassembles Task 1's concatenated
    multi-member blob exactly as `gzip.decompress` does — this is just the
    same decompression, read incrementally instead of all at once. Yields
    nothing (an empty generator) when the turn has no transcript."""
    transcript = TurnTranscript.objects.filter(turn=turn).first()
    if transcript is None or not transcript.raw_jsonl_gz:
        return
    with gzip.GzipFile(fileobj=io.BytesIO(bytes(transcript.raw_jsonl_gz))) as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


#: How much of a turn's transcript the reading view parses. A prefix, not the
#: whole thing: the view exists to show what a turn did, and a turn long enough
#: to blow past this is read in full from the raw route instead.
TRANSCRIPT_VIEW_MAX_MESSAGES = 500


def iter_transcript_lines(turn: Turn):
    """`iter_transcript`, re-cut at newlines and decoded — one JSONL line at a
    time, still without materializing the whole blob."""
    pending = b""
    for chunk in iter_transcript(turn):
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            yield line.decode("utf-8", errors="replace")
    if pending:
        yield pending.decode("utf-8", errors="replace")


def transcript_messages(
    turn: Turn, *, max_messages: int = TRANSCRIPT_VIEW_MAX_MESSAGES
) -> tuple[list[dict], bool]:
    """A turn's retained transcript as readable messages — the same shape, parser
    and secret scrub as a shared session page — plus whether it was cut short.

    This is how you see what a CLOUD-runner agent turn did: it runs one-shot
    `claude -p` with no canopy Session to stream into, so the transcript on the
    turn is the only record of its work. The raw route stays the byte-exact
    source; this is a bounded reading view over it."""
    from apps.session_sharing import parser, redact

    parsed = parser.parse_lines(iter_transcript_lines(turn), max_turns=max_messages)
    messages = []
    for index, t in enumerate(parsed.turns):
        plaintext, content, _ = redact.redact_turn(t.plaintext, t.content)
        messages.append(
            {"turn_index": index, "role": t.role, "content": content, "plaintext": plaintext}
        )
    return messages, len(parsed.turns) >= max_messages
