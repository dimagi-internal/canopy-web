"""Unit tests for the invite service layer (`apps/workspaces/services.py`).

These exercise `create_invite` / `accept_invite` / `revoke_invite` /
`pending_invite_for_email` directly (no HTTP), complementing the
request-level coverage in `test_members.py` (which must keep passing
unchanged — same status codes, same behavior, now backed by this layer).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.workspaces.models import Workspace, WorkspaceInvite, WorkspaceMembership
from apps.workspaces.services import (
    InviteError,
    accept_invite,
    create_invite,
    pending_invite_for_email,
    revoke_invite,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _ws(owner, slug="acme"):
    return Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)


def test_create_then_accept_sets_membership_at_invited_role():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    invitee = _user("b@dimagi.com")
    result_ws, role = accept_invite(token=inv.token, user=invitee)

    assert result_ws == ws
    assert role == "editor"
    assert WorkspaceMembership.objects.get(workspace=ws, user=invitee).role == "editor"
    inv.refresh_from_db()
    assert inv.accepted_at is not None


def test_create_invite_reuses_live_pending_invite_for_same_workspace_and_email():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    first = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    second = create_invite(workspace=ws, email="b@dimagi.com", role="viewer", invited_by=owner)

    assert first.id == second.id
    assert WorkspaceInvite.objects.filter(workspace=ws, email="b@dimagi.com").count() == 1


def test_create_invite_normalizes_email_to_lowercase():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="B@Dimagi.com", role="editor", invited_by=owner)
    assert inv.email == "b@dimagi.com"


def test_accept_expired_invite_raises_expired():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    inv.expires_at = timezone.now() - dt.timedelta(days=1)
    inv.save(update_fields=["expires_at"])

    invitee = _user("b@dimagi.com")
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token=inv.token, user=invitee)
    assert exc_info.value.code == "expired"


def test_accept_revoked_invite_raises_revoked():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=inv)

    invitee = _user("b@dimagi.com")
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token=inv.token, user=invitee)
    assert exc_info.value.code == "revoked"


def test_accept_wrong_email_raises_email_mismatch():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    wrong_user = _user("c@dimagi.com")
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token=inv.token, user=wrong_user)
    assert exc_info.value.code == "email_mismatch"


def test_accept_unknown_token_raises_not_found():
    someone = _user("b@dimagi.com")
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token="does-not-exist", user=someone)
    assert exc_info.value.code == "not_found"


def test_accept_when_already_member_at_different_role_keeps_existing_role_but_marks_accepted():
    """Deliberate semantic: accept is get_or_create on membership. If the invitee
    somehow already holds a DIFFERENT role in the workspace (e.g. an owner invited
    to re-join as editor, or a second invite for a role bump landed late), acceptance
    does NOT change their existing role — but the invite itself is still consumed
    (marked accepted) so it can't be replayed."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    # owner already has role "owner" in ws; invite them at a different role ("viewer")
    inv = create_invite(workspace=ws, email="a@dimagi.com", role="viewer", invited_by=owner)

    result_ws, role = accept_invite(token=inv.token, user=owner)

    assert result_ws == ws
    # returned role reflects the EXISTING membership role, not the invite's role
    assert role == "owner"
    assert WorkspaceMembership.objects.get(workspace=ws, user=owner).role == "owner"
    inv.refresh_from_db()
    assert inv.accepted_at is not None


def test_revoke_invite_is_idempotent():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    revoke_invite(invite=inv)
    first_revoked_at = WorkspaceInvite.objects.get(pk=inv.pk).revoked_at
    revoke_invite(invite=inv)  # calling again must not raise or change the timestamp
    second_revoked_at = WorkspaceInvite.objects.get(pk=inv.pk).revoked_at

    assert first_revoked_at == second_revoked_at


def test_revoke_invite_does_not_reopen_an_accepted_invite():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    invitee = _user("b@dimagi.com")
    accept_invite(token=inv.token, user=invitee)

    fresh = WorkspaceInvite.objects.get(pk=inv.pk)  # reflects the acceptance just committed
    revoke_invite(invite=fresh)  # must be a no-op: an accepted invite can't be revoked

    fresh.refresh_from_db()
    assert fresh.accepted_at is not None
    assert fresh.revoked_at is None


def test_pending_invite_for_email_ignores_expired_revoked_and_accepted():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)

    expired = create_invite(workspace=ws, email="expired@dimagi.com", role="editor", invited_by=owner)
    expired.expires_at = timezone.now() - dt.timedelta(days=1)
    expired.save(update_fields=["expires_at"])

    revoked = create_invite(workspace=ws, email="revoked@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=revoked)

    accepted = create_invite(workspace=ws, email="accepted@dimagi.com", role="editor", invited_by=owner)
    accept_invite(token=accepted.token, user=_user("accepted@dimagi.com"))

    assert pending_invite_for_email("expired@dimagi.com") is None
    assert pending_invite_for_email("revoked@dimagi.com") is None
    assert pending_invite_for_email("accepted@dimagi.com") is None


def test_pending_invite_for_email_is_case_insensitive_and_returns_live_invite():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    found = pending_invite_for_email("B@Dimagi.com")

    assert found is not None
    assert found.id == inv.id
