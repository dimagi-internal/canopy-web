"""Live transcript push for sessions a viewer is actually watching.

Active only while `stream_desired` is set server-side, and holding NO durable
resume state: the server's `last_index` is the checkpoint (spec 2026-07-24)."""
from __future__ import annotations

import logging
from pathlib import Path

from . import chat_bridge, hooks, transcript
from .client import Client
from .config import Config
from .failure_log import note_failure, note_success
from .tail import TailReader

logger = logging.getLogger("canopy_runner")


# Per-session live-stream tailers, keyed by session_id — active only while a viewer
# is attached (stream_desired on the server). Distinct from _tail_readers (the idle
# tail read-model that fills RunnerBinding.tail); this is the live push to attached
# viewers. Each entry: {"reader": TailReader|None, "count": int (records consumed ==
# the next record's ordinal), "session_key": str, "project": str, "last_index":
# int|None (the server's catch-up marker)}. Deliberately holds NO durable resume
# state — the server's last_index is the checkpoint (spec 2026-07-24).
_stream_readers: dict[str, dict] = {}



def post_stream_rows(cfg: Config, client: Client, sid: str, rows: list[dict]) -> bool:
    """Ship conversational rows as live events. seq == index (the composite
    transcript ordinal): monotonic per session forever, so the WS-derived
    `seq:<n>` message ids can never collide across detaches, restarts, or
    failovers — including between two rows of the same transcript record."""
    events = [
        {"kind": r["role"], "seq": r["index"], "index": r["index"],
         "payload": chat_bridge.row_payload(r)}
        for r in rows
    ]
    try:
        client.post_session_stream(cfg.runner_id, sid, events)
        note_success(f"stream:{sid}")
        return True
    except Exception:  # noqa: BLE001
        note_failure(f"stream:{sid}", "stream post")
        return False


def sync_session_streams(cfg: Config, client: Client) -> None:
    """Tail each session a viewer is watching and ship every new conversational
    record (user + assistant) with its transcript ordinal — the server persists
    them as the session's durable Message rows and fans the assistant frames out
    live (spec 2026-07-24).

    The resume point is SERVER-side: the descriptor's `last_index` (max persisted
    turn_index). On attach we read the transcript once and ship everything after
    it; steady state stays change-driven off TailReader (only newly-appended
    bytes). There is deliberately NO local offset checkpoint — a failed post just
    drops the tailer so the next tick re-attaches from the marker, and a runner
    restart or account failover recovers identically. Best-effort — a client
    hiccup never breaks a tick."""
    try:
        streams = client.sync_streams(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("stream sync failed (non-fatal)", exc_info=True)
        return
    desired = {s["session_id"]: s for s in streams if s.get("session_id")}
    home = Path.home()
    claude_home = home / ".claude" / "projects"

    for sid in list(_stream_readers):  # drop tailers for sessions no longer watched
        if sid not in desired:
            _stream_readers.pop(sid, None)

    # The hook path resolves a cwd against these, so refresh it wholesale here
    # rather than accumulating stale entries for detached sessions.
    hooks._hook_sessions.clear()
    for sid, s in desired.items():
        hooks._hook_sessions[(s.get("project") or "", s.get("session_key") or "")] = sid
        st = _stream_readers.setdefault(sid, {
            "reader": None, "count": 0,
            "session_key": s.get("session_key") or "", "project": s.get("project") or "",
        })
        # Refresh every tick: the marker advances as the server persists our posts.
        st["last_index"] = s.get("last_index")

    for sid, st in _stream_readers.items():
        reader = st["reader"]
        if reader is None:
            # (Re-)attach: read the whole file once, atomically w.r.t. this reader,
            # and catch up from the server marker. No marker yet -> stream forward
            # only (history stays the backfill's job).
            path = transcript.resolve_transcript(
                st["project"], st["session_key"], home=home, claude_home=claude_home
            )
            if not path:
                continue  # transcript wasn't there yet — retry resolving next tick
            reader = TailReader(str(path))
            records = reader.read_new()
            last = st["last_index"]
            since = chat_bridge.end_index(len(records)) if last is None else int(last)
            rows = chat_bridge.conversational_messages(records, since)
            if rows and not post_stream_rows(cfg, client, sid, rows):
                continue  # nothing consumed; re-attach next tick
            st["reader"], st["count"] = reader, len(records)
            continue
        new_records = reader.read_new()
        if not new_records:
            continue
        base = st["count"]
        # The batch's records start at `base` in the file; the offset is applied
        # to the RECORD ordinal inside compose_index, never to the composite
        # index (adding it there would shift a row into another record's slots).
        rows = chat_bridge.conversational_messages(new_records, -1, record_offset=base)
        if rows and not post_stream_rows(cfg, client, sid, rows):
            # Don't advance past unshipped records: reset so the next tick
            # re-attaches and catches up from the server marker.
            st["reader"], st["count"] = None, 0
            continue
        st["count"] = base + len(new_records)


def drain_backfills(cfg: Config, client: Client) -> None:
    """Ship full transcript history — with ordinals, so the server upsert-fills the
    older rows around anything the live stream already persisted. Best-effort — a
    missing transcript or a client hiccup is skipped, not fatal."""
    try:
        backfills = client.sync_backfills(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("backfill sync failed (non-fatal)", exc_info=True)
        return
    home = Path.home()
    claude_home = home / ".claude" / "projects"
    for b in backfills:
        sid = b.get("session_id")
        path = transcript.resolve_transcript(
            b.get("project") or "", b.get("session_key") or "", home=home, claude_home=claude_home
        )
        if not (sid and path):
            continue  # transcript not resolvable -> leave it; server keeps showing the tail
        messages = chat_bridge.conversational_messages(chat_bridge.read_records(path), -1)
        try:
            client.post_session_backfill(cfg.runner_id, sid, messages)
            note_success(f"backfill:{sid}")
        except Exception:  # noqa: BLE001
            # A backfill that keeps failing never rebuilds the session's history
            # and never stops trying — exactly the case that must not be silent.
            note_failure(f"backfill:{sid}", f"backfill post ({len(messages)} rows)")


