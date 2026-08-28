"""Transcript push for every session this runner backs.

Runs for ALL of them, not only the watched ones: the transcript is the durable
source (spec 2026-07-24), so persisting it must not depend on someone having the
chat open. `stream_desired` survives server-side as the LIVE fan-out gate only.
Holds NO durable resume state — the server's `first_index`/`last_index` are the
checkpoint."""
from __future__ import annotations

import logging
from pathlib import Path

# Both live in the library BOTH runners share, so the laptop and the cloud box
# cannot drift on either question (what to ship, and how big a request may be).
from canopy_transcript import chunk_rows, rows_to_ship

from . import chat_bridge, hooks, transcript
from .client import Client
from .config import Config
from .failure_log import note_failure, note_success
from .tail import TailReader

logger = logging.getLogger("canopy_runner")


# Per-session transcript tailers, keyed by session_id — one for every session this
# runner backs. Distinct from _tail_readers (the idle tail read-model that fills
# RunnerBinding.tail); this is the durable push, which also feeds live viewers.
# Each entry: {"reader": TailReader|None, "count": int (records consumed == the next
# record's ordinal), "session_key": str, "project": str, "first_index"/"last_index":
# int|None (the bounds of what the server holds)}. Deliberately holds NO durable
# resume state — those server-side markers are the checkpoint (spec 2026-07-24).
_stream_readers: dict[str, dict] = {}



def post_stream_rows(
    cfg: Config, client: Client, sid: str, rows: list[dict], transcript_id: str = ""
) -> bool:
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
        # Chunked for the same reason the backfill is: a first-attach ship now
        # carries a session's whole history, which on the longest transcripts
        # exceeds the server's 2.5 MB request ceiling and dies as an unhandled 500.
        for batch in chunk_rows(events) or [[]]:
            client.post_session_stream(cfg.runner_id, sid, batch, transcript_id)
        note_success(f"stream:{sid}")
        return True
    except Exception:  # noqa: BLE001
        note_failure(f"stream:{sid}", "stream post")
        return False


def sync_session_streams(cfg: Config, client: Client) -> None:
    """Tail EVERY session this runner backs and ship its conversational records
    (user + assistant) with their transcript ordinals — the server persists them as
    the session's durable Message rows, and fans them out live only for the ones a
    viewer has open (spec 2026-07-24).

    Every session, not just the watched ones. While the server only asked about
    attached sessions, a session's durable history was a side effect of somebody
    having looked at it: labs held 16% of its rows, 8 of 12 sessions at zero, and
    the gap was papered over by asking the runner at read time ("Load full
    session"), which is what made that button slow and unreliable. Reading and
    parsing a 6.5 MB transcript costs ~29 ms, so there was never a cost reason to
    wait to be asked.

    The resume point is SERVER-side: the descriptor's `first_index`/`last_index`
    (the oldest and newest turn_index it holds). On attach we read the transcript
    once and then either ship everything after `last_index` (the server has the
    head, just catch it up) or ship the whole history (it does not, so appending
    could never repair it). Steady state stays change-driven off TailReader (only
    newly-appended bytes). There is deliberately NO local offset checkpoint — a
    failed post just drops the tailer so the next tick re-attaches from the marker,
    and a runner restart or account failover recovers identically. Best-effort — a
    client hiccup never breaks a tick."""
    try:
        streams = client.sync_streams(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("stream sync failed (non-fatal)", exc_info=True)
        return
    desired = {s["session_id"]: s for s in streams if s.get("session_id")}
    home = Path.home()
    claude_home = home / ".claude" / "projects"

    for sid in list(_stream_readers):  # drop tailers for sessions this runner no longer backs
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
        # Refresh every tick: the markers advance as the server persists our posts.
        st["last_index"] = s.get("last_index")
        st["first_index"] = s.get("first_index")
        # ...and WHICH transcript they are markers into. See below.
        st["server_transcript_id"] = s.get("transcript_id") or ""

    for sid, st in _stream_readers.items():
        # Resolve EVERY tick, not just on attach. A binding is keyed on the emdash
        # task NAME and names are reused, so the session under a live reader can be
        # swapped for a different conversation without anything here changing —
        # after which the reader tails a file nobody is writing to, forever. Costs
        # a scandir per session per tick against a warm dentry cache.
        path = transcript.resolve_transcript(
            st["project"], st["session_key"], home=home, claude_home=claude_home,
            emdash_db=cfg.emdash_db,
        )
        if not path:
            continue  # transcript wasn't there yet — retry resolving next tick
        transcript_id = path.stem  # the Claude session uuid — the conversation's identity
        if st["reader"] is not None and st.get("transcript_id") != transcript_id:
            # The task behind this session was replaced. Start over on the new file:
            # its ordinals are a fresh space, and the server drops what it holds the
            # moment it sees the new id (issue #615).
            st["reader"], st["count"] = None, 0
        st["transcript_id"] = transcript_id
        reader = st["reader"]
        if reader is None:
            # (Re-)attach: read the whole file once, atomically w.r.t. this
            # reader, and ship whatever the server is missing — the whole history
            # when it has no head, otherwise just what is past its marker.
            reader = TailReader(str(path))
            records = reader.read_new()
            # The server's markers are ordinals into the transcript it named. If
            # that is not this file, they license nothing — a shorter successor
            # sits entirely below the predecessor's high-water mark and would be
            # suppressed in full. Discard them and ship everything; the server
            # replaces its rows rather than merging into them.
            same = bool(st.get("server_transcript_id")) and (
                st["server_transcript_id"] == transcript_id
            )
            rows = rows_to_ship(
                chat_bridge.conversational_messages(records, -1),
                first_held=st.get("first_index") if same else None,
                last_held=st.get("last_index") if same else None,
            )
            if rows and not post_stream_rows(cfg, client, sid, rows, transcript_id):
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
        if rows and not post_stream_rows(cfg, client, sid, rows, transcript_id):
            # Don't advance past unshipped records: reset so the next tick
            # re-attaches and catches up from the server marker.
            st["reader"], st["count"] = None, 0
            continue
        st["count"] = base + len(new_records)


def drain_closes(cfg: Config, client: Client) -> None:
    """Close any session we have been asked to close and have not.

    The twin of `drain_menu_answers`, for the other verb that only ever existed
    as a WS control frame. Verified 2026-08-01: a real close returned
    `{"ok":true,"closing":true}` from the API and the runner logged nothing at
    all, so the emdash task stayed open and the session stayed active.

    Idempotent by construction — closing a task that is already gone is a no-op,
    and the server retires the request when the task stops being reported.
    """
    from . import close as close_mod

    try:
        closes = client.sync_closes(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("close sync failed (non-fatal)", exc_info=True)
        return
    for c in closes:
        session_key = c.get("session_key") or ""
        if not session_key:
            continue
        try:
            close_mod.close_session(session_key, cdp_port=cfg.cdp_port)
            logger.info("closed %s from the poll tick", session_key)
        except Exception:  # noqa: BLE001
            # Left set: the server clears it when the task stops being reported,
            # so a failure here simply retries next tick.
            logger.debug("close from the poll tick failed for %s", session_key, exc_info=True)


def drain_menu_answers(cfg: Config, client: Client) -> None:
    """Press any answer a human has given that has not reached us yet.

    The WS control frame is the fast path and usually wins. This is what makes an
    answer SURVIVE the channel being down: a frame published into a group with no
    consumer is discarded silently, while the runner keeps heartbeating over REST
    and so still reads ONLINE — the API says `ok:true` and the tap simply
    evaporates. Measured on labs 2026-08-01: an answer sent at 10:50 never
    arrived, between control-channel reconnects at 10:16 and 10:58.

    Best-effort, like every other drain here: a failure is retried next tick,
    because the server only retires an answer once we report on it.
    """
    from . import hooks

    try:
        answers = client.sync_menu_answers(cfg.runner_id)
    except Exception:  # noqa: BLE001
        logger.debug("menu-answer sync failed (non-fatal)", exc_info=True)
        return
    for a in answers:
        session_key, answer_id = a.get("session_key") or "", a.get("answer_id") or ""
        if not (session_key and answer_id):
            continue
        outcome, screen = hooks.answer_menu(session_key, a.get("option"),
                                            selections=a.get("selections"),
                                            texts=a.get("texts"),
                                            cdp_port=cfg.cdp_port)
        hooks.note_answer_outcome(session_key, outcome, screen)
        try:
            client.post_menu_answer_result(cfg.runner_id, a.get("session_id") or "",
                                           answer_id, outcome)
        except Exception:  # noqa: BLE001
            # Leave it set: pressing twice is worse than pressing late, and the
            # re-read before every press is what makes the retry safe.
            logger.debug("could not retire menu answer %s (non-fatal)", answer_id,
                         exc_info=True)
        else:
            logger.info("menu answer for %s applied from the poll tick (%s)",
                        session_key, outcome)


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
            b.get("project") or "", b.get("session_key") or "", home=home,
            claude_home=claude_home, emdash_db=cfg.emdash_db,
        )
        if not (sid and path):
            continue  # transcript not resolvable -> leave it; server keeps showing the tail
        messages = chat_bridge.conversational_messages(chat_bridge.read_records(path), -1)
        # `or [[]]` so an empty transcript still posts once: the ask is only
        # retired by a final chunk, and a session with nothing to ship must not
        # leave the request set forever.
        batches = chunk_rows(messages) or [[]]
        try:
            for i, batch in enumerate(batches):
                client.post_session_backfill(
                    cfg.runner_id, sid, batch, final=(i == len(batches) - 1),
                    # Names the conversation this history belongs to, so a rebuild
                    # REPLACES a predecessor's rows instead of upsert-filling into
                    # them under a shared ordinal space (issue #615).
                    transcript_id=path.stem,
                )
            note_success(f"backfill:{sid}")
        except Exception:  # noqa: BLE001
            # A backfill that keeps failing never rebuilds the session's history
            # and never stops trying — exactly the case that must not be silent.
            note_failure(f"backfill:{sid}", f"backfill post ({len(messages)} rows)")


