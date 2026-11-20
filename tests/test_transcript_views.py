"""The core read (`transcript_read.read`): one function, named views, one scrub, paging that never splits a row;
and `GET /api/canopy-sessions/{id}?view=` on it, the default unchanged."""
from __future__ import annotations

import pytest
from django.test import Client

from apps.canopy_sessions import transcript_read as tr
from apps.canopy_sessions.models import Message, Session
from tests.fixtures.brain_world import member, user

pytestmark = pytest.mark.django_db

SECRET = "ghp_" + "0123456789abcdefABCDEF" + "0123456789abcd"


@pytest.fixture()
def s():
    from apps.workspaces.models import Workspace

    alice = user("alice")
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=alice)
    member(alice, ws)
    session = Session.objects.create(workspace=ws, created_by=alice, origin=Session.ORIGIN_WEB, title="t")
    rows = [(Message.USER, f"deploy with {SECRET}", {}, {"user_id": alice.pk, "name": "Alice"}),
            (Message.USER, "<system-reminder>harness says hi", {}, None),
            (Message.TOOL_USE, "", {"id": "t1", "name": "Bash", "input": {"command": "make"}}, None),
            (Message.TOOL_RESULT, "ok", {"tool_use_id": "t1", "is_error": False}, None),
            (Message.ASSISTANT, "done " + "x" * 700, {}, None),
            (Message.USER, "thanks", {}, {"user_id": alice.pk, "name": "Alice"})]
    for i, (role, text, content, author) in enumerate(rows):
        Message.objects.create(session=session, role=role, turn_index=i, plaintext=text, content=content,
                               author=author)
    session.alice = alice
    return session


def _texts(result):
    return [m["text"] for m in result.messages]


def test_each_view_is_a_subset_of_the_one_before_it(s):
    full = tr.read(s)
    assert [m["role"] for m in full.messages] == ["user", "user", "tool_use", "tool_result", "assistant", "user"]
    assert full.messages[2] == {"index": 2, "role": "tool_use", "text": '{"command": "make"}', "author": None,
                                "tool": {"name": "Bash"}}
    assert full.messages[3]["tool"] == {"name": "Bash"}
    assert full.messages[0]["author"] == {"user_id": s.alice.pk, "name": "Alice"}
    conv = tr.read(s, view=tr.CONVERSATION)
    assert [m["index"] for m in conv.messages] == [0, 4, 5]                  # harness noise and tool rows dropped
    assert [m["index"] for m in tr.read(s, view=tr.TYPED).messages] == [0, 5]
    opening = tr.read(s, view=tr.OPENING)
    assert [m["index"] for m in opening.messages] == [0, 5]
    assert tr.narrower(tr.FULL, tr.TYPED) == tr.TYPED == tr.narrower(tr.TYPED, tr.CONVERSATION)


def test_scrub_is_the_one_redaction_and_off_by_default(s):
    assert SECRET in _texts(tr.read(s))[0]
    assert SECRET not in _texts(tr.read(s, scrub=True))[0] and "redacted" in _texts(tr.read(s, scrub=True))[0]


def test_paging_by_characters_never_splits_a_row_and_resumes(s):
    page = tr.read(s, max_chars=30)
    assert page.truncated and page.next_after is not None
    seen = list(page.messages)
    while page.truncated:
        page = tr.read(s, after=page.next_after, max_chars=30)
        seen += page.messages
    assert [m["index"] for m in seen] == list(range(6))
    assert any(len(m["text"]) > 700 for m in seen)                       # a row longer than a page comes whole
    assert len(tr.read(s, limit=2).messages) == 2


def test_the_session_route_names_a_view_and_its_default_is_the_tail_as_before(s):
    c = Client()
    c.force_login(s.alice)
    default = c.get(f"/api/canopy-sessions/{s.pk}").json()
    assert [m["turn_index"] for m in default["messages"]] == list(range(6))
    typed = c.get(f"/api/canopy-sessions/{s.pk}", {"view": "typed"}).json()
    assert [m["turn_index"] for m in typed["messages"]] == [0, 5]
    assert c.get(f"/api/canopy-sessions/{s.pk}", {"view": "nope"}).status_code == 422
