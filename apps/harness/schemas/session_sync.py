"""Session stream and backfill schemas."""
from __future__ import annotations

import uuid

from ninja import Schema


class StreamDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str
    # The server-side catch-up marker: max persisted turn_index for the session
    # (None = no rows yet). The runner ships transcript records AFTER this on
    # attach, so a restart/failover never loses the resume point.
    last_index: int | None = None
    # The OLDEST turn_index the server holds (None = no rows). Paired with
    # last_index so the runner can distinguish "you are up to date, send what's
    # new" from "you are missing the head, send everything": a max alone only ever
    # licenses appending above the high-water mark, so a session whose beginning
    # was never captured could never repair itself no matter how long it streamed.
    first_index: int | None = None
    # Whether a viewer is attached. Governs live fan-out ONLY — rows are persisted
    # for every session either way (see list_streams). Old runners ignore it, which
    # is safe: they simply keep streaming exactly the sessions they already did.
    live: bool = True
    # WHICH transcript the first_index/last_index markers above were computed from
    # (the Claude session uuid). The markers are per-file ordinals, so they mean
    # nothing against a different file — a runner that resolves a transcript whose
    # id disagrees with this must ignore them and ship the whole history. "" = the
    # server has no provenance for its rows, which is treated the same way.
    transcript_id: str = ""
    # The box's owner opted in to coaching (a live SessionReviewGrant) and this is
    # their own, non-sensitive session: ship its whole transcript, even once it is
    # no longer active (apps/canopy_sessions/coaching.py). Revoking the last grant
    # clears it on the next poll.
    ship_full: bool = False
    # The session is no longer active (only listed when `ship_full`): once its
    # history has shipped there is nothing more to tail.
    archived: bool = False


class StreamSyncOut(Schema):
    streams: list[StreamDescriptorOut] = []


class LiveEventIn(Schema):
    kind: str
    seq: int
    # Transcript record ordinal (raw index into the session's .jsonl). -1 = an
    # old runner that doesn't send ordinals; such events stay live-view-only
    # (persisting assistant-only rows would kill the tail fallback's user side).
    index: int = -1
    payload: dict = {}
    # The transcript record's `cwd` and `gitBranch`, which Claude Code stamps on
    # every record. Folded into Session.activity (canopy_sessions.activity), never
    # stored per row. "" = an old runner, or a record that carried none.
    cwd: str = ""
    git_branch: str = ""


class SessionStreamIn(Schema):
    session_id: uuid.UUID
    events: list[LiveEventIn] = []
    # The transcript these ordinals index into (the Claude session uuid). When it
    # differs from what the binding recorded, the session's derived rows belong to
    # a DIFFERENT conversation and are dropped before this batch is written — see
    # canopy_sessions.transcript_ingest.ensure_transcript_identity. "" = an old runner,
    # which makes no provenance claim and is left alone.
    transcript_id: str = ""


class StreamPostOut(Schema):
    count: int


# ---------------------------------------------------------------------------
# On-demand backfill — the runner-facing half (Plan 3 Task 6)
# ---------------------------------------------------------------------------


class BackfillDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str


class BackfillSyncOut(Schema):
    backfills: list[BackfillDescriptorOut] = []


class BackfillMessageIn(Schema):
    role: str
    text: str = ""
    # Transcript record ordinal. -1 = an old runner; the server then keeps the
    # legacy write-once contract (sequential, only into an empty session).
    index: int = -1
    # Structured fields for a non-prose row — a tool_use's {id,name,input}, a
    # tool_result's {tool_use_id,is_error}. Empty for plain text. Stored as the
    # Message's content so history renders identically to the live stream.
    content: dict = {}
    # Same as LiveEventIn's: the record's cwd / gitBranch, for Session.activity.
    cwd: str = ""
    git_branch: str = ""


class SessionBackfillIn(Schema):
    session_id: uuid.UUID
    messages: list[BackfillMessageIn] = []
    # False = "more chunks follow"; the server keeps `backfill_requested` set so a
    # ship that dies halfway is retried whole rather than leaving a partial history
    # behind a cleared flag. A transcript has to be chunked at all because the whole
    # payload would go in ONE request against DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB),
    # which Django raises as an unhandled 500 BEFORE the view runs — measured over
    # 193 local transcripts, one already exceeds it and three more are past 1.9 MB.
    # Defaults True so an old runner, which posts exactly once, is unaffected.
    final: bool = True
    # The transcript this history came from — same contract as SessionStreamIn's.
    # Only the FIRST chunk can drop anything: once it records the id, the rest of
    # the ship matches and writes straight through, so a chunked backfill can
    # never delete the chunks that preceded it.
    transcript_id: str = ""


class BackfillWriteOut(Schema):
    written: int


# ---------------------------------------------------------------------------
# Deferred turns — what a task runs when it is approved
# ---------------------------------------------------------------------------
