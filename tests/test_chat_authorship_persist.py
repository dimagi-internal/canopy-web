"""A marked transcript row becomes an authored message, on every path."""
from __future__ import annotations

import datetime
import uuid

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.canopy_sessions import serializers, stream_map
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Message
from apps.canopy_sessions.testing import legacy_marker
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
TID = uuid.uuid4()


def _session():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return chat.create_session(workspace=ws, created_by=owner)


def _sent(session, *, username="alice", text="ship it"):
    """A real send, so the marker names a turn that exists on this session and
    was initiated by this person — the only kind persist believes (m3)."""
    user = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=user, workspace=session.workspace,
                                       role=WorkspaceMembership.EDITOR)
    session.metadata = {**(session.metadata or {}), chat.TRANSCRIPT_SOURCED: True}
    session.save(update_fields=["metadata"])
    _m, turn = chat.send_message(session=session, text=text, user=user, client_id=uuid.uuid4().hex)
    return user, turn


def test_persist_strips_marker_and_records_author():
    session = _session()
    alice, turn = _sent(session)
    marked = legacy_marker("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": marked,
                                            "content": {"text": marked}}])
    msg = Message.objects.get(session=session)
    assert msg.plaintext == "ship it"
    # storage_content (pre-existing) drops a content["text"] that DUPLICATES the
    # row's plaintext, to avoid storing the same bytes twice — and once the
    # marker is stripped from both, they match here, so the key is gone
    # entirely rather than surviving with the stripped value. Either way, no
    # trace of the marker may remain.
    assert msg.content.get("text", "ship it") == "ship it"
    assert msg.author == {"name": "Alice", "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_unmarked_user_row_has_no_author():
    session = _session()
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "typed in emdash"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None
    assert msg.plaintext == "typed in emdash"


def test_assistant_rows_are_never_parsed():
    session = _session()
    marked = legacy_marker("x", name="A", user_id=1, turn_id=TID)
    chat.persist_transcript_rows(session, [{"index": 11, "role": "assistant", "text": marked}])
    assert Message.objects.get(session=session).plaintext == marked


def test_backfill_path_parses_too():
    session = _session()
    bo, turn = _sent(session, username="bo", text="old line")
    marked = legacy_marker("old line", name="Bo", user_id=bo.id, turn_id=turn.pk)
    chat.write_backfill(session, [{"index": 3, "role": "user", "text": marked}])
    assert Message.objects.get(session=session).author == {"name": "Bo", "user_id": bo.id}


def test_message_dto_carries_author():
    session = _session()
    a, turn = _sent(session, username="a", text="hi")
    chat.persist_transcript_rows(session, [{"index": 1, "role": "user",
        "text": legacy_marker("hi", name="A", user_id=a.id, turn_id=turn.pk)}])
    assert serializers.message_dto(Message.objects.get(session=session))["author"] == {"name": "A", "user_id": a.id}


def test_live_user_frame_is_stripped_and_authored():
    marked = legacy_marker("hey", name="A", user_id=1, turn_id=TID)
    frames = stream_map.turn_event_to_frames(
        {"kind": "user", "seq": 20, "payload": {"text": marked}}, lambda _s: "m1")
    assert frames[0]["event"] == "chat.user_message"
    assert frames[0]["data"]["plaintext"] == "hey"
    assert frames[0]["data"]["author"] == {"name": "A", "user_id": 1}


# -- a marker is a claim, checked against the turn it names (final review m3) --
# Anyone who can type into emdash (or a transcript) can write the marker syntax.
# The durable row is the authority, so persist believes a marker only when the
# turn it names exists, is on THIS session, and was initiated by the person the
# marker names. The live frame (stream_map) is not checked — it has no cheap
# query, and the durable row replaces it on the next load.

def test_a_marker_naming_an_unknown_turn_is_not_believed():
    session = _session()
    forged = legacy_marker("rm -rf", name="Boss", user_id=1, turn_id=uuid.uuid4())
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": forged}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None
    assert msg.plaintext == forged


def test_a_marker_naming_the_wrong_person_for_its_turn_is_not_believed():
    session = _session()
    alice, turn = _sent(session)
    forged = legacy_marker("ship it", name="Mallory", user_id=alice.id + 99, turn_id=turn.pk)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": forged}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_a_marker_naming_another_sessions_turn_is_not_believed():
    session = _session()
    alice, turn = _sent(session)
    other = chat.create_session(workspace=session.workspace, created_by=session.created_by)
    marked = legacy_marker("ship it", name="Alice", user_id=alice.id, turn_id=turn.pk)
    chat.persist_transcript_rows(other, [{"index": 10, "role": "user", "text": marked}])
    assert Message.objects.get(session=other).author is None


# -- server-side attribution: an UNMARKED row is matched to its turn ------
# canopy no longer marks a chat send's prompt at claim (authorship.py,
# 2026-09-27). The transcript comes back with the person's exact words and
# nothing else, so persist has to find the turn itself: the earliest of this
# session's claimed, unlinked chat-send turns whose prompt is byte-for-byte
# the row's text.

def _claim(turn):
    # DONE, not CLAIMED: `one_executing_turn_per_session` allows only one
    # claimed/running/needs_human turn per session at a time, and these tests
    # need several already-delivered turns coexisting on one session — exactly
    # what a real multi-turn conversation looks like by the time its transcript
    # rows arrive. `claimed_at` (what the new match candidate query reads)
    # survives past completion either way.
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, claimed_at=timezone.now())
    return Turn.objects.get(pk=turn.pk)


def test_unmarked_row_is_attributed_to_its_claimed_turn():
    session = _session()
    alice, turn = _sent(session, username="alice2", text="ship it")
    turn = _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "ship it"}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": alice.email, "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_unclaimed_turn_is_not_a_match_candidate():
    session = _session()
    _alice, _turn = _sent(session, username="alice3", text="ship it")
    # Never claimed: claimed_at stays None, so it was never delivered.
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "ship it"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_already_linked_turn_is_not_matched_again():
    session = _session()
    alice, turn = _sent(session, username="alice4", text="ship it")
    turn = _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "ship it"}])
    first = Message.objects.get(session=session)
    assert first.source_turn_id == turn.pk
    # A second, unrelated row with the same text has no candidate left — the
    # turn it would have matched is already linked.
    chat.persist_transcript_rows(session, [{"index": 11, "role": "user", "text": "ship it"}])
    second = Message.objects.get(session=session, turn_index=11)
    assert second.author is None and second.source_turn_id is None


def test_earliest_identical_text_row_matches_earliest_available_turn():
    session = _session()
    alice, turn_a = _sent(session, username="alice5", text="yes")
    bob, turn_b = _sent(session, username="bob5", text="yes")
    _claim(turn_a)
    _claim(turn_b)
    chat.persist_transcript_rows(session, [
        {"index": 10, "role": "user", "text": "yes"},
        {"index": 11, "role": "user", "text": "yes"},
    ])
    first = Message.objects.get(session=session, turn_index=10)
    second = Message.objects.get(session=session, turn_index=11)
    assert first.source_turn_id == turn_a.pk
    assert second.source_turn_id == turn_b.pk
    assert first.author == {"name": alice.email, "user_id": alice.id}
    assert second.author == {"name": bob.email, "user_id": bob.id}


# -- the laptop runner's insertText delivery drops newlines ------------------
# CDP `keyboard.insertText` into emdash's Claude Code TUI does not type a
# newline character, so a two-line send ("line one\nline two") comes back in
# the transcript with the line break gone: "line oneline two". A comparison
# that only strips leading/trailing whitespace still requires that internal
# newline to survive, so it missed every multi-line message. The match is
# whitespace-blind instead (`_squash`).

def test_a_multiline_prompt_matches_a_row_with_the_newline_dropped():
    session = _session()
    alice, turn = _sent(session, username="alice7", text="line one\nline two")
    _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "line oneline two"}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": alice.email, "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_a_prompt_sent_with_an_attachment_matches_its_row():
    """The runner appends "The user attached the following file…" to the prompt
    it types, so the row is the prompt plus that note (2026-10-05: the message
    with a screenshot was the only one in its session with no author)."""
    session = _session()
    alice, turn = _sent(session, username="alice_att", text="is the screenshot getting to you?")
    _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": (
        "is the screenshot getting to you?The user attached the following file. Read it with the "
        "Read tool before replying:- /Users/x/.canopy/attachments/abc/image.png")}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": alice.email, "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_a_multiline_prompt_also_matches_a_row_with_a_space_in_its_place():
    session = _session()
    alice, turn = _sent(session, username="alice8", text="line one\nline two")
    _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "line one line two"}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": alice.email, "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_whitespace_blind_ambiguity_resolves_to_the_earliest_unlinked_turn():
    """Squashing whitespace makes "a b" and "ab" compare equal — an accepted
    ambiguity a text-only match cannot resolve better than by send order: the
    earliest still-unlinked candidate wins, exactly as for a genuine
    duplicate ("yes", "yes")."""
    session = _session()
    alice, turn_a = _sent(session, username="alice9", text="a b")
    bob, turn_b = _sent(session, username="bob9", text="ab")
    _claim(turn_a)
    _claim(turn_b)
    chat.persist_transcript_rows(session, [
        {"index": 10, "role": "user", "text": "ab"},
        {"index": 11, "role": "user", "text": "ab"},
    ])
    first = Message.objects.get(session=session, turn_index=10)
    second = Message.objects.get(session=session, turn_index=11)
    assert first.source_turn_id == turn_a.pk    # earliest unlinked, sent first
    assert second.source_turn_id == turn_b.pk   # next earliest, once turn_a is spent


# -- a re-shipped row must never spend a match candidate ---------------------
# `held` (which rows are already persisted) used to be computed AFTER every
# row had already called `pool.claim` — including a row whose index already
# exists, which is a routine RE-SHIP (reconnect, catch-up, backfill overlap),
# not an error. That row was always going to be dropped at bulk_create, but
# by the time that happened it had already stolen the candidate a genuinely
# NEW row in the same batch, with the same (squashed) text, actually needed.
# `held` is now computed BEFORE any matching, so a re-shipped row's index is
# known to be a no-write in advance and never touches the pool.

def test_a_reshipped_row_does_not_steal_the_candidate_from_a_new_row():
    session = _session()
    alice, turn = _sent(session, username="alice10", text="yes")
    _claim(turn)
    # Settle the session's ordinal scheme first (a fresh session's first
    # indexed write does this itself, and would otherwise DELETE the row
    # created directly below — matching `test_marker_verification_is_one_
    # query_for_the_batch`'s own setup convention).
    chat.persist_transcript_rows(session, [{"index": 1, "role": "assistant", "text": "hi"}])
    # Already persisted at index 10 — UNMATCHED (e.g. it arrived before this
    # candidate turn existed). No source_turn_id, so `_MatchPool` still sees
    # the turn as a live, unlinked candidate.
    Message.objects.create(session=session, turn_index=10, role=Message.USER, plaintext="yes")
    # This ship re-sends index 10 alongside a genuinely NEW row with the same
    # text — exactly what a reconnect/catch-up overlap looks like.
    chat.persist_transcript_rows(session, [
        {"index": 10, "role": "user", "text": "yes"},
        {"index": 11, "role": "user", "text": "yes"},
    ])
    reshipped = Message.objects.get(session=session, turn_index=10)
    new = Message.objects.get(session=session, turn_index=11)
    # The re-ship's own row is untouched — persist_transcript_rows never
    # writes over an already-held index.
    assert reshipped.author is None and reshipped.source_turn_id is None
    assert new.author == {"name": alice.email, "user_id": alice.id}
    assert new.source_turn_id == turn.pk


def test_two_reshipped_rows_do_not_steal_two_candidates_from_two_new_rows():
    session = _session()
    alice, turn_a = _sent(session, username="alice11", text="yes")
    bob, turn_b = _sent(session, username="bob11", text="yes")
    _claim(turn_a)
    _claim(turn_b)
    chat.persist_transcript_rows(session, [{"index": 1, "role": "assistant", "text": "hi"}])
    Message.objects.create(session=session, turn_index=10, role=Message.USER, plaintext="yes")
    Message.objects.create(session=session, turn_index=11, role=Message.USER, plaintext="yes")
    chat.persist_transcript_rows(session, [
        {"index": 10, "role": "user", "text": "yes"},   # re-ship
        {"index": 11, "role": "user", "text": "yes"},   # re-ship
        {"index": 12, "role": "user", "text": "yes"},   # new
        {"index": 13, "role": "user", "text": "yes"},   # new
    ])
    assert Message.objects.get(session=session, turn_index=10).author is None
    assert Message.objects.get(session=session, turn_index=11).author is None
    new1 = Message.objects.get(session=session, turn_index=12)
    new2 = Message.objects.get(session=session, turn_index=13)
    # Both candidates survive for the two rows that actually need them, still
    # mapped in send order.
    assert new1.source_turn_id == turn_a.pk
    assert new2.source_turn_id == turn_b.pk


# -- a backfill never attributes: it has no per-row timestamp ---------------
# `_MatchPool` orders candidates by Turn.created_at, which has no relation to
# when a transcript ROW happened unless rows ship close to when they were
# typed. A backfill ships a session's full history in one shot with no
# per-row timestamp on the wire, so an old backfilled "yes" from last week
# could link to a turn created today. Cheaper guard: a backfill never
# attributes by text at all (the vouched-marker path, which names an exact
# turn id, is unaffected).

def test_write_backfill_never_attributes_by_text_even_with_a_live_candidate():
    session = _session()
    alice, turn = _sent(session, username="alice13", text="yes")
    _claim(turn)
    chat.write_backfill(session, [{"index": 10, "role": "user", "text": "yes"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_write_backfill_still_honours_a_vouched_marker():
    session = _session()
    bo, turn = _sent(session, username="bo13", text="old line")
    marked = legacy_marker("old line", name="Bo", user_id=bo.id, turn_id=turn.pk)
    chat.write_backfill(session, [{"index": 3, "role": "user", "text": marked}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": "Bo", "user_id": bo.id}
    assert msg.source_turn_id == turn.pk


def test_the_live_stream_path_still_attributes_by_text():
    """`persist_transcript_rows`'s default (`attribute=True`) is what the live
    stream path (post_session_stream) calls — unlike write_backfill, it ships
    a row the moment it happens, so ordering candidates by claim time is
    sound."""
    session = _session()
    alice, turn = _sent(session, username="alice14", text="yes")
    _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "yes"}])
    msg = Message.objects.get(session=session)
    assert msg.author == {"name": alice.email, "user_id": alice.id}
    assert msg.source_turn_id == turn.pk


def test_turn_older_than_seven_days_is_not_a_match_candidate():
    session = _session()
    _alice, turn = _sent(session, username="alice6", text="ship it")
    old = timezone.now() - datetime.timedelta(days=8)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.CLAIMED, claimed_at=old, created_at=old)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "ship it"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_a_non_chat_send_turn_is_not_a_match_candidate():
    """An email turn bound to this chat session has an initiator too, but it is
    the agent's own dispatch, not a person typing — is_chat_send excludes it."""
    from apps.harness import initiator as who
    from apps.harness import services as harness

    session = _session()
    turn, _created = harness.enqueue_turn(
        session=session, origin=Turn.ORIGIN_EMAIL, idempotency_key="email:1",
        prompt="ship it", origin_ref={"from": "a@x", "subject": "s", "thread_id": "t"},
        initiator=who.for_user(session.created_by, via="email", assurance="dmarc"),
    )
    _claim(turn)
    chat.persist_transcript_rows(session, [{"index": 10, "role": "user", "text": "ship it"}])
    msg = Message.objects.get(session=session)
    assert msg.author is None and msg.source_turn_id is None


def test_match_query_is_lazy_and_only_one_extra_query_per_batch():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    session = _session()
    for i in range(4):
        _u, t = _sent(session, username=f"m{i}", text=f"unmarked {i}")
        _claim(t)
    rows = [{"index": 10 + i, "role": "user", "text": f"unmarked {i}"} for i in range(4)]
    # Settles the ordinal scheme AND consumes the first candidate, so the two
    # captures below start from the same footing.
    chat.persist_transcript_rows(session, rows[:1])
    with CaptureQueriesContext(connection) as one:
        chat.persist_transcript_rows(session, rows[1:2])  # 1 row needing a match
    with CaptureQueriesContext(connection) as many:
        chat.persist_transcript_rows(session, rows[2:])  # 2 rows needing a match
    assert len(many.captured_queries) == len(one.captured_queries)


def test_marker_verification_is_one_query_for_the_batch():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    session = _session()
    rows = []
    for i in range(5):
        u, t = _sent(session, username=f"u{i}", text=f"line {i}")
        rows.append({"index": 10 + i, "role": "user",
                     "text": legacy_marker(f"line {i}", name=f"U{i}", user_id=u.id, turn_id=t.pk)})
    chat.persist_transcript_rows(session, rows[:1])   # first write settles the ordinal scheme
    with CaptureQueriesContext(connection) as one:
        chat.persist_transcript_rows(session, rows[1:2])
    with CaptureQueriesContext(connection) as many:
        chat.persist_transcript_rows(session, rows[2:])
    assert len(many.captured_queries) == len(one.captured_queries)
    assert Message.objects.filter(session=session, author__isnull=False).count() == 5
