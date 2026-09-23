"""SP3 Task 2 — participants, presence, and co-edited draft services."""
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

def test_update_bumps_version_and_records_editor():
    owner, _ws, session = _ctx()
    d = drafts.update_draft(session, expected_version=0, body="hello", editor=owner)
    assert d.version == 1
    assert d.body == "hello"
    assert d.last_editor_id == owner.id


def test_version_mismatch_raises_with_authoritative_state():
    owner, _ws, session = _ctx()
    drafts.update_draft(session, expected_version=0, body="a", editor=owner)
    with pytest.raises(drafts.DraftVersionMismatch) as exc:
        drafts.update_draft(session, expected_version=0, body="stale", editor=owner)
    assert exc.value.current_version == 1
    assert exc.value.current_body == "a"


def test_live_lock_blocks_others_then_take_over_after_release():
    owner, ws, session = _ctx()
    other = User.objects.create_user("o", "o@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    participants.ensure_participant(session, other)
    presence.touch(session.id, owner.id)
    presence.touch(session.id, other.id)
    # owner edits and is present -> holds a LIVE lock; other is blocked
    drafts.update_draft(session, expected_version=0, body="mine", editor=owner)
    with pytest.raises(drafts.DraftLockHeld):
        drafts.update_draft(session, expected_version=1, body="theirs", editor=other)
    # you can't yank a live lock either
    with pytest.raises(drafts.DraftLockHeld):
        drafts.take_over(session, editor=other)
    # owner leaves -> lock frees -> other takes the baton and edits
    presence.leave(session.id, owner.id)
    drafts.take_over(session, editor=other)
    d = drafts.update_draft(session, expected_version=1, body="theirs", editor=other)
    assert d.body == "theirs"


def test_lock_frees_when_holder_not_present():
    owner, ws, session = _ctx()
    other = User.objects.create_user("o", "o@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    participants.ensure_participant(session, other)
    # owner edits but is NOT present -> lock is not held; other can edit freely
    drafts.update_draft(session, expected_version=0, body="mine", editor=owner)
    d = drafts.update_draft(session, expected_version=1, body="theirs", editor=other)
    assert d.body == "theirs"


def test_commit_returns_text_and_resets_draft():
    owner, _ws, session = _ctx()
    drafts.update_draft(session, expected_version=0, body="send me", editor=owner)
    text = drafts.commit_active_draft(session)
    assert text == "send me"
    d = drafts.active_draft(session)
    assert d.body == ""
    assert d.version == 2
