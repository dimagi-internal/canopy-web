"""The author marker: the ONE syntax that carries who-said-it through Claude's transcript.

Production no longer WRITES this marker (canopy no longer prepends it to a
delivered prompt — see authorship.py's module docstring, updated 2026-09-27).
`parse` still reads it, forever, because rows already recorded — and any
transcript backfilled around the change — still carry it. `legacy_marker`
(apps/canopy_sessions/testing.py) builds a realistic marked string the way the
old `authorship.mark` used to, so these tests keep exercising the real format.
"""
from __future__ import annotations

import uuid

import pytest

from apps.canopy_sessions import authorship
from apps.canopy_sessions.testing import legacy_marker

TID = uuid.UUID("3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8")


def test_round_trip_user():
    marked = legacy_marker("hello there", name="Alice Smith", user_id=42, turn_id=TID)
    assert marked.splitlines()[0] == '[canopy from="Alice Smith" user=42 turn=3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8]'
    author, bare, tid = authorship.parse(marked)
    assert author == {"name": "Alice Smith", "user_id": 42}
    assert bare == "hello there"
    assert tid == TID.hex


def test_round_trip_contact():
    marked = legacy_marker("hi", name="Beth", contact_id=7, turn_id=TID)
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": "Beth", "contact_id": 7}
    assert bare == "hi"


def test_multiline_body_survives():
    marked = legacy_marker("line one\nline two", name="A", user_id=1, turn_id=TID)
    assert authorship.parse(marked)[1] == "line one\nline two"


def test_unmarked_text_is_untouched():
    assert authorship.parse("just typed in emdash") == (None, "just typed in emdash", None)


def test_marker_mid_text_is_not_parsed():
    quoted = 'look at this:\n[canopy from="X" user=1 turn=' + TID.hex + ']\nweird'
    assert authorship.parse(quoted) == (None, quoted, None)


@pytest.mark.parametrize("line", [
    '[canopy from="A" user=x turn=' + TID.hex + ']',        # non-int id
    '[canopy from="A" user=1 turn=abc]',                    # short turn
    '[canopy from="A" turn=' + TID.hex + ']',               # no id
    ' [canopy from="A" user=1 turn=' + TID.hex + ']',       # leading space
])
def test_parse_rejects_malformed_marker(line):
    text = line + "\nbody"
    assert authorship.parse(text) == (None, text, None)


def test_name_with_quote_and_newline_round_trips():
    marked = legacy_marker("x", name='Pat "PJ" O\\Brien\nJr', user_id=3, turn_id=TID)
    assert len(marked.splitlines()) == 2  # marker stays ONE line
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": 'Pat "PJ" O\\Brien Jr', "user_id": 3}
    assert bare == "x"


def test_marker_only_no_body():
    marked = legacy_marker("", name="A", user_id=1, turn_id=TID)
    assert authorship.parse(marked) == ({"name": "A", "user_id": 1}, "", TID.hex)


# -- the live bug this file's rewrite fixes (2026-09-27) --------------------
# The laptop runner types a prompt into emdash as ONE line, so a marker's
# trailing "\n" is lost in transit and the row arrives as
# `[canopy from="…" user=N turn=…]Are you working?` — marker and body glued
# together with no separator. `parse` must still find the marker and recover
# the bare body: the marker itself is still exactly at the start of the text,
# just not followed by a newline.

def test_parse_accepts_marker_with_no_trailing_newline():
    glued = '[canopy from="X" user=1 turn=' + TID.hex + ']Are you working?'
    author, bare, tid = authorship.parse(glued)
    assert author == {"name": "X", "user_id": 1}
    assert bare == "Are you working?"
    assert tid == TID.hex


from types import SimpleNamespace


def _turn(**kw):
    base = dict(pk=TID, prompt="do it", chat_session_id=None,
                initiator_user_id=None, initiator_user=None,
                initiator_contact_id=None, initiator_contact=None)
    base.update(kw)
    return SimpleNamespace(**base)


# -- which turns are chat sends (final review C1) ---------------------------
# `is_chat_send` still decides which turns are a PERSON'S chat line — used by
# `queued_messages` and by the new server-side attribution match in
# `services.persist_transcript_rows`. It no longer feeds a marking function
# (that function is gone), but the predicate itself is unchanged.

def _chat_turn(**kw):
    user = SimpleNamespace(get_full_name=lambda: "Alice Smith", email="a@x")
    base = dict(chat_session_id=uuid.uuid4(), initiator_user_id=42, initiator_user=user,
                origin="canopy_web_chat", idempotency_key="chat:abc:c1")
    base.update(kw)
    return _turn(**base)


@pytest.mark.parametrize("origin", ["canopy_web_chat", "slack", "ace_web"])
def test_chat_origins_are_chat_sends(origin):
    assert authorship.is_chat_send(_chat_turn(origin=origin)) is True


@pytest.mark.parametrize("origin", ["email", "canopy_scheduler", "api"])
def test_non_chat_origins_are_never_chat_sends(origin):
    assert authorship.is_chat_send(_chat_turn(origin=origin)) is False


def test_a_contacts_widget_send_naming_api_is_still_a_chat_send():
    contact = SimpleNamespace(display_name="Beth", email="b@x")
    t = _chat_turn(origin="api", initiator_user_id=None, initiator_user=None,
                   initiator_contact_id=7, initiator_contact=contact)
    assert authorship.is_chat_send(t) is True


def test_a_session_turn_that_is_not_a_send_is_not_a_chat_send():
    # A transfer's preamble is canopy's words, not the person's.
    assert authorship.is_chat_send(_chat_turn(idempotency_key="transfer:a:b:1")) is False


def test_author_of_reads_the_initiator():
    user = SimpleNamespace(get_full_name=lambda: "Alice Smith", email="a@x")
    assert authorship.author_of(_turn(initiator_user_id=42, initiator_user=user)) == {
        "name": "Alice Smith", "user_id": 42,
    }


def test_author_of_is_none_with_no_initiator():
    assert authorship.author_of(_turn()) is None
