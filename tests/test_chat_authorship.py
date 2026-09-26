"""The author marker: the ONE syntax that carries who-said-it through Claude's transcript."""
from __future__ import annotations

import uuid

import pytest

from apps.canopy_sessions import authorship

TID = uuid.UUID("3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8")


def test_round_trip_user():
    marked = authorship.mark("hello there", name="Alice Smith", user_id=42, turn_id=TID)
    assert marked.splitlines()[0] == '[canopy from="Alice Smith" user=42 turn=3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8]'
    author, bare, tid = authorship.parse(marked)
    assert author == {"name": "Alice Smith", "user_id": 42}
    assert bare == "hello there"
    assert tid == TID.hex


def test_round_trip_contact():
    marked = authorship.mark("hi", name="Beth", contact_id=7, turn_id=TID)
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": "Beth", "contact_id": 7}
    assert bare == "hi"


def test_multiline_body_survives():
    marked = authorship.mark("line one\nline two", name="A", user_id=1, turn_id=TID)
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
def test_parse_requires_exact_first_line(line):
    text = line + "\nbody"
    assert authorship.parse(text) == (None, text, None)


def test_name_with_quote_and_newline_round_trips():
    marked = authorship.mark("x", name='Pat "PJ" O\\Brien\nJr', user_id=3, turn_id=TID)
    assert len(marked.splitlines()) == 2  # marker stays ONE line
    author, bare, _ = authorship.parse(marked)
    assert author == {"name": 'Pat "PJ" O\\Brien Jr', "user_id": 3}
    assert bare == "x"


def test_marker_only_no_body():
    marked = authorship.mark("", name="A", user_id=1, turn_id=TID)
    assert authorship.parse(marked) == ({"name": "A", "user_id": 1}, "", TID.hex)


from types import SimpleNamespace


def _turn(**kw):
    base = dict(pk=TID, prompt="do it", chat_session_id=None,
                initiator_user_id=None, initiator_user=None,
                initiator_contact_id=None, initiator_contact=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_for_turn_leaves_non_chat_turns_alone():
    assert authorship.for_turn(_turn()) == "do it"


def test_for_turn_marks_a_chat_turn_with_its_initiator():
    user = SimpleNamespace(get_full_name=lambda: "Alice Smith", email="a@x")
    out = authorship.for_turn(_turn(chat_session_id=uuid.uuid4(), initiator_user_id=42, initiator_user=user))
    assert authorship.parse(out) == ({"name": "Alice Smith", "user_id": 42}, "do it", TID.hex)


def test_for_turn_without_initiator_is_unmarked():
    assert authorship.for_turn(_turn(chat_session_id=uuid.uuid4())) == "do it"
