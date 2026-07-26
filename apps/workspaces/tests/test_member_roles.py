"""Unit tests for the member-role service layer (`apps/workspaces/services.py`).

These exercise `set_member_role` / `is_last_owner` directly (no HTTP),
complementing the request-level coverage in `test_members.py` (RBAC + status
codes), mirroring the split `test_invites.py` already uses for invites.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.services import MemberError, is_last_owner, set_member_role

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _ws(owner, slug="acme"):
    ws = Workspace.objects.create(slug=slug, display_name=slug.title(), created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role=WorkspaceMembership.OWNER)
    return ws


def test_set_member_role_promotes_a_member():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    viewer = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)

    m = set_member_role(workspace=ws, user_id=viewer.id, role=WorkspaceMembership.EDITOR)

    assert m.role == WorkspaceMembership.EDITOR
    assert WorkspaceMembership.objects.get(workspace=ws, user=viewer).role == WorkspaceMembership.EDITOR


def test_set_member_role_demotes_a_member():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    second_owner = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=second_owner, role=WorkspaceMembership.OWNER)

    m = set_member_role(workspace=ws, user_id=second_owner.id, role=WorkspaceMembership.VIEWER)

    assert m.role == WorkspaceMembership.VIEWER


def test_set_member_role_is_idempotent():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)

    m = set_member_role(workspace=ws, user_id=owner.id, role=WorkspaceMembership.OWNER)

    assert m.role == WorkspaceMembership.OWNER


def test_set_member_role_raises_not_found_for_non_member():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    stranger = _user("z@dimagi.com")

    with pytest.raises(MemberError) as exc_info:
        set_member_role(workspace=ws, user_id=stranger.id, role=WorkspaceMembership.EDITOR)
    assert exc_info.value.code == "not_found"


def test_set_member_role_raises_last_owner_when_demoting_the_only_owner():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)

    with pytest.raises(MemberError) as exc_info:
        set_member_role(workspace=ws, user_id=owner.id, role=WorkspaceMembership.EDITOR)
    assert exc_info.value.code == "last_owner"
    assert WorkspaceMembership.objects.get(workspace=ws, user=owner).role == WorkspaceMembership.OWNER


def test_set_member_role_allows_demoting_one_of_two_owners():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    second_owner = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=second_owner, role=WorkspaceMembership.OWNER)

    m = set_member_role(workspace=ws, user_id=owner.id, role=WorkspaceMembership.VIEWER)

    assert m.role == WorkspaceMembership.VIEWER


def test_is_last_owner_true_only_for_the_sole_owner():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    m = WorkspaceMembership.objects.get(workspace=ws, user=owner)
    assert is_last_owner(m) is True

    second_owner = _user("b@dimagi.com")
    m2 = WorkspaceMembership.objects.create(workspace=ws, user=second_owner, role=WorkspaceMembership.OWNER)
    m.refresh_from_db()
    assert is_last_owner(m) is False
    assert is_last_owner(m2) is False


def test_is_last_owner_false_for_a_non_owner():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    viewer = _user("b@dimagi.com")
    m = WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    assert is_last_owner(m) is False
