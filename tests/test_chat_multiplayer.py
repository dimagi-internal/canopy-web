"""SP3 Task 2 — participants, presence, and per-author draft services."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User

from apps.canopy_sessions import drafts, participants, presence
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import SessionParticipant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    session = chat.create_session(workspace=ws, created_by=owner)
    return owner, ws, session


# -- participants --

def test_creator_is_owner():
    owner, _ws, session = _ctx()
    assert participants.role_for(session, owner) == SessionParticipant.OWNER


def test_a_workspace_member_is_not_in_someone_elses_chat():
    """Being in the workspace is the tenant gate, not a grant to every chat in
    it. The socket used to auto-join any member as an editor, which turned one
    connection into durable access to a conversation REST hid from them."""
    owner, ws, session = _ctx()
    teammate = User.objects.create_user("t", "t@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=teammate, workspace=ws, role=WorkspaceMembership.EDITOR)
    assert participants.can_access(session, teammate) is False
    assert participants.role_for(session, teammate) is None
    assert not SessionParticipant.objects.filter(session=session, user=teammate).exists()


def test_a_shared_teammate_gets_the_role_they_were_given():
    owner, ws, session = _ctx()
    teammate = User.objects.create_user("t", "t@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=teammate, workspace=ws, role=WorkspaceMembership.EDITOR)
    participants.ensure_participant(session, teammate, SessionParticipant.VIEWER)
    assert participants.can_access(session, teammate) is True
    assert participants.role_for(session, teammate) == SessionParticipant.VIEWER


def test_non_member_denied():
    _owner, _ws, session = _ctx()
    outsider = User.objects.create_user("no", "no@dimagi.com", "pw")
    assert participants.can_access(session, outsider) is False


# -- presence --

def test_presence_touch_and_leave():
    owner, _ws, session = _ctx()
    assert presence.present_ids(session.id) == set()
    presence.touch(session.id, owner.id)
    assert owner.id in presence.present_ids(session.id)
    presence.leave(session.id, owner.id)
    assert owner.id not in presence.present_ids(session.id)


def test_presence_expiry():
    owner, _ws, session = _ctx()
    presence.touch(session.id, owner.id, ttl=-1)  # already expired
    assert presence.present_ids(session.id) == set()


# -- draft co-editing --

def _two():
    owner, ws, session = _ctx()
    other = User.objects.create_user("o", "o@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    return owner, other, session


def test_two_people_draft_at_once_without_blocking():
    owner, other, session = _two()
    a = drafts.update_draft(session, user=owner, expected_version=0, body="mine")
    b = drafts.update_draft(session, user=other, expected_version=0, body="theirs")
    assert (a.body, b.body) == ("mine", "theirs")
    assert a.pk != b.pk


def test_version_guard_is_per_author():
    owner, _other, session = _two()
    drafts.update_draft(session, user=owner, expected_version=0, body="a")
    with pytest.raises(drafts.DraftVersionMismatch) as exc:
        drafts.update_draft(session, user=owner, expected_version=0, body="stale")
    assert exc.value.current_body == "a"


def test_commit_takes_only_my_text():
    owner, other, session = _two()
    drafts.update_draft(session, user=owner, expected_version=0, body="send me")
    drafts.update_draft(session, user=other, expected_version=0, body="not yet")
    assert drafts.commit_draft(session, owner) == "send me"
    assert drafts.draft_for(session, owner).body == ""
    assert drafts.draft_for(session, other).body == "not yet"


def test_peer_drafts_excludes_me_and_empty():
    owner, other, session = _two()
    drafts.update_draft(session, user=other, expected_version=0, body="typing")
    drafts.draft_for(session, owner)  # exists, empty
    assert [d.author_id for d in drafts.peer_drafts(session, owner)] == [other.id]
    assert drafts.peer_drafts(session, other) == []
