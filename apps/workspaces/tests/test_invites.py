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


def test_create_invite_reusing_a_still_pending_row_at_a_different_role_updates_role_and_rotates_token():
    """Regression: an owner invites b@ as owner, immediately realizes the
    mistake, and re-invites b@ as editor. The prior behavior returned the
    SAME row still holding role="owner" (only a lapsed/expired row got its
    role rewritten on reuse) — so the owner would copy a link that still
    reads "owner", and since accept is now upgrade-only, b (already an
    editor) accepting it would be silently promoted to owner. A still-live
    reuse at a genuinely different role must re-arm the row (fresh token,
    fresh expiry, the newly-requested role) exactly like the lapsed-TTL path
    does, so the copied link always reflects the LAST-requested role."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    first = create_invite(workspace=ws, email="b@dimagi.com", role="owner", invited_by=owner)
    old_token = first.token

    second = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    assert second.id == first.id  # still the same row, not a sibling
    assert second.role == "editor"
    assert second.token != old_token
    assert second.is_pending()
    # the stale (pre-rotation) token no longer resolves to anything live
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token=old_token, user=_user("someone-else@dimagi.com"))
    assert exc_info.value.code == "not_found"


def test_create_invite_reusing_a_still_pending_row_at_the_same_role_is_a_true_no_op():
    """The counterpart to the regression above: re-issuing an invite at the
    SAME role it already holds must not rotate the token (no reason to
    invalidate a link nothing about it actually changed)."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    first = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    second = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    assert second.id == first.id
    assert second.token == first.token
    assert second.role == "editor"


def test_create_invite_after_expiry_reuses_row_with_fresh_token_and_is_pending_again():
    """Regression: an ordinary re-invite after the 14-day TTL lapses (never
    accepted, never revoked) must NOT try to create a sibling row — the
    partial unique constraint's condition only excludes accepted/revoked rows,
    not expired ones, so a naive re-check-then-create collides with the
    still-"live"-per-the-constraint expired row and 500s. Re-arm the same row
    instead: same id, fresh token, fresh expiry, pending again."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    first = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    old_token = first.token
    first.expires_at = timezone.now() - dt.timedelta(days=1)
    first.save(update_fields=["expires_at"])

    second = create_invite(workspace=ws, email="b@dimagi.com", role="viewer", invited_by=owner)

    assert second.id == first.id  # re-armed the same row, not a new sibling
    assert WorkspaceInvite.objects.filter(workspace=ws, email="b@dimagi.com").count() == 1
    assert second.is_pending()
    assert second.role == "viewer"  # the newly-requested role wins on re-arm
    assert second.token != old_token  # fresh token
    # the stale token no longer resolves to a pending invite
    with pytest.raises(InviteError) as exc_info:
        accept_invite(token=old_token, user=_user("someone-else@dimagi.com"))
    assert exc_info.value.code == "not_found"


def test_create_invite_after_revoke_still_returns_a_working_invite():
    """Locks in existing (already-correct) behavior: re-inviting someone whose
    prior invite was revoked creates a brand-new usable invite (a revoked row
    is never reused/reopened)."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    first = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=first)

    second = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    assert second.id != first.id
    assert second.is_pending()
    invitee = _user("b@dimagi.com")
    result_ws, role = accept_invite(token=second.token, user=invitee)
    assert result_ws == ws
    assert role == "editor"


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


def test_accept_when_invited_at_lower_role_than_existing_keeps_existing_role_but_marks_accepted():
    """Deliberate semantic: accept is UPGRADE-ONLY on membership role (never a
    demotion). If the invitee already holds a HIGHER role than the invite offers
    (e.g. an owner invited to re-join as viewer, or a stray lower-role invite
    landed late), acceptance does NOT change their existing role — but the
    invite itself is still consumed (marked accepted) so it can't be replayed.
    Explicit demotion is the owner-only PATCH `/members/{user_id}/`, never a
    side effect of someone accepting an invite."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    # owner already has role "owner" in ws; invite them at a LOWER role ("viewer")
    inv = create_invite(workspace=ws, email="a@dimagi.com", role="viewer", invited_by=owner)

    result_ws, role = accept_invite(token=inv.token, user=owner)

    assert result_ws == ws
    # returned role reflects the EXISTING (higher) membership role, not the invite's role
    assert role == "owner"
    assert WorkspaceMembership.objects.get(workspace=ws, user=owner).role == "owner"
    inv.refresh_from_db()
    assert inv.accepted_at is not None


def test_accept_when_invited_at_higher_role_than_existing_upgrades_role():
    """The upgrade half of the same semantic: an existing viewer invited as
    editor ends up an editor (upgrade), and the invite is marked accepted."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    viewer = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    result_ws, role = accept_invite(token=inv.token, user=viewer)

    assert result_ws == ws
    assert role == "editor"
    assert WorkspaceMembership.objects.get(workspace=ws, user=viewer).role == "editor"
    inv.refresh_from_db()
    assert inv.accepted_at is not None


def test_accept_when_invited_at_same_role_as_existing_leaves_role_unchanged():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    editor = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=editor, role=WorkspaceMembership.EDITOR)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    result_ws, role = accept_invite(token=inv.token, user=editor)

    assert result_ws == ws
    assert role == "editor"
    assert WorkspaceMembership.objects.get(workspace=ws, user=editor).role == "editor"
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


def test_pending_invite_for_email_ignores_expired():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    expired = create_invite(workspace=ws, email="expired@dimagi.com", role="editor", invited_by=owner)
    expired.expires_at = timezone.now() - dt.timedelta(days=1)
    expired.save(update_fields=["expires_at"])

    assert pending_invite_for_email("expired@dimagi.com") is None


def test_pending_invite_for_email_ignores_revoked():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    revoked = create_invite(workspace=ws, email="revoked@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=revoked)

    assert pending_invite_for_email("revoked@dimagi.com") is None


def test_pending_invite_for_email_ignores_accepted():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    accepted = create_invite(workspace=ws, email="accepted@dimagi.com", role="editor", invited_by=owner)
    accept_invite(token=accepted.token, user=_user("accepted@dimagi.com"))

    assert pending_invite_for_email("accepted@dimagi.com") is None


def test_pending_invite_for_email_is_case_insensitive_and_returns_live_invite():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    found = pending_invite_for_email("B@Dimagi.com")

    assert found is not None
    assert found.id == inv.id
