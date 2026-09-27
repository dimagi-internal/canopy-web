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
    # Only a PRESENT author's draft is someone typing (final review I4).
    presence.touch(session.id, other.id)
    presence.touch(session.id, owner.id)
    assert [d.author_id for d in drafts.peer_drafts(session, owner)] == [other.id]
    assert drafts.peer_drafts(session, other) == []


# -- per-person typing visibility --
#
# The mode is its OWN idempotent frame (`set_visibility`), never a field on
# the version-guarded `update_draft` — a stale keystroke echo racing a mode
# change used to be able to downgrade it server-side (canopy-ui#…
# "hidden->live->hidden" regression).

def test_set_visibility_applies_with_no_version_and_does_not_bump_version():
    owner, _other, session = _two()
    before = drafts.update_draft(session, user=owner, expected_version=0, body="hi")
    d = drafts.set_visibility(session, owner, "typing")
    assert d.visibility == "typing"
    assert d.version == before.version  # untouched — this is not an edit


def test_set_visibility_ignores_an_invalid_value():
    owner, _other, session = _two()
    d = drafts.set_visibility(session, owner, "loud")
    assert d.visibility == "live"  # default, unchanged — invalid values are ignored


def test_update_draft_no_longer_changes_visibility():
    owner, _other, session = _two()
    drafts.set_visibility(session, owner, "hidden")
    # A stale keystroke frame — even one still carrying a `visibility` field
    # from an in-flight 0.14 client — cannot touch the mode any more.
    d = drafts.update_draft(session, user=owner, expected_version=0, body="a")
    assert d.visibility == "hidden"


def test_peer_drafts_excludes_hidden():
    owner, other, session = _two()
    drafts.update_draft(session, user=other, expected_version=0, body="secret")
    drafts.set_visibility(session, other, "hidden")
    presence.touch(session.id, other.id)
    presence.touch(session.id, owner.id)
    assert drafts.peer_drafts(session, owner) == []


def test_peer_drafts_still_includes_typing_mode():
    owner, other, session = _two()
    drafts.update_draft(session, user=other, expected_version=0, body="whisper")
    drafts.set_visibility(session, other, "typing")
    presence.touch(session.id, other.id)
    presence.touch(session.id, owner.id)
    peers = drafts.peer_drafts(session, owner)
    assert [d.author_id for d in peers] == [other.id]
    # The row itself still carries the real body; the DTO is what blanks it.
    assert peers[0].body == "whisper"


def test_set_visibility_does_not_touch_updated_at():
    # `updated_at` is the keystroke clock `peer_drafts` reads for freshness;
    # a mode switch is not a keystroke.
    owner, _other, session = _two()
    before = drafts.update_draft(session, user=owner, expected_version=0, body="hi")
    after = drafts.set_visibility(session, owner, "hidden")
    after.refresh_from_db()
    assert after.visibility == "hidden"
    assert after.updated_at == before.updated_at


def test_switching_an_old_draft_to_live_does_not_resurface_it():
    from datetime import timedelta

    from apps.canopy_sessions.models import Draft
    from django.utils import timezone

    owner, other, session = _two()
    d = drafts.update_draft(session, user=other, expected_version=0, body="stale words")
    drafts.set_visibility(session, other, "hidden")
    Draft.objects.filter(pk=d.pk).update(updated_at=timezone.now() - timedelta(minutes=11))
    drafts.set_visibility(session, other, "live")
    presence.touch(session.id, other.id)
    presence.touch(session.id, owner.id)
    assert drafts.peer_drafts(session, owner) == []
