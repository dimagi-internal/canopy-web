"""Unit tests for the member-role service layer (`apps/workspaces/services.py`).

These exercise `set_member_role` / `is_last_owner` directly (no HTTP),
complementing the request-level coverage in `test_members.py` (RBAC + status
codes), mirroring the split `test_invites.py` already uses for invites.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from django.db.models.query import QuerySet

from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.services import MemberError, is_last_owner, remove_member, set_member_role

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


def test_set_member_role_rejects_a_role_outside_role_rank():
    """Defense in depth for a caller that bypasses the Pydantic Literal at the
    HTTP boundary (shell, management command, MCP tool) — the service itself
    must not persist a role .save() lets through but ROLE_RANK can't look up
    (accept_invite's ROLE_RANK[m.role] would otherwise KeyError/500 on the
    very next invite acceptance for this workspace)."""
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    with pytest.raises(MemberError) as exc_info:
        set_member_role(workspace=ws, user_id=owner.id, role="superuser")
    assert exc_info.value.code == "invalid_role"
    assert WorkspaceMembership.objects.get(workspace=ws, user=owner).role == WorkspaceMembership.OWNER


# ---- remove_member (moved into the service so it shares the same
# transaction + row-lock boundary as set_member_role — see the TOCTOU tests
# below) ----


def test_remove_member_removes_a_non_owner():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    viewer = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)

    remove_member(workspace=ws, user_id=viewer.id)

    assert not WorkspaceMembership.objects.filter(workspace=ws, user=viewer).exists()


def test_remove_member_raises_not_found_for_non_member():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    stranger = _user("z@dimagi.com")
    with pytest.raises(MemberError) as exc_info:
        remove_member(workspace=ws, user_id=stranger.id)
    assert exc_info.value.code == "not_found"


def test_remove_member_raises_last_owner_when_removing_the_only_owner():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    with pytest.raises(MemberError) as exc_info:
        remove_member(workspace=ws, user_id=owner.id)
    assert exc_info.value.code == "last_owner"
    assert WorkspaceMembership.objects.filter(workspace=ws, user=owner).exists()


def test_remove_member_allows_removing_one_of_two_owners():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    second_owner = _user("b@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=second_owner, role=WorkspaceMembership.OWNER)

    remove_member(workspace=ws, user_id=owner.id)

    assert not WorkspaceMembership.objects.filter(workspace=ws, user=owner).exists()


# ---- TOCTOU guard: the last-owner check must take a row lock ----
#
# The real hazard (two concurrent requests each observing count(owner)==2
# before either commits, so BOTH pass the guard and the workspace ends with
# zero owners) can only be demonstrated against a database that actually
# enforces row locking under concurrent transactions/connections — this repo's
# test suite runs on sqlite, which silently no-ops `select_for_update()`
# (`connection.features.has_select_for_update` is False; Django drops the
# FOR UPDATE clause instead of erroring — see apps/harness/services.py's
# `append_events` for the same caveat). These tests are honest about that
# limit: they assert the code path actually TAKES the lock (a spy on
# `QuerySet.select_for_update`), not that concurrent transactions serialize.
# The serialization itself is exercised in production by Postgres.


def test_set_member_role_last_owner_guard_takes_a_row_lock(monkeypatch):
    calls: list[type] = []
    original = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        calls.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)

    owner = _user("a@dimagi.com")
    ws = _ws(owner)

    with pytest.raises(MemberError):
        set_member_role(workspace=ws, user_id=owner.id, role=WorkspaceMembership.EDITOR)

    assert WorkspaceMembership in calls


def test_remove_member_last_owner_guard_takes_a_row_lock(monkeypatch):
    calls: list[type] = []
    original = QuerySet.select_for_update

    def spy(self, *args, **kwargs):
        calls.append(self.model)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", spy)

    owner = _user("a@dimagi.com")
    ws = _ws(owner)

    with pytest.raises(MemberError):
        remove_member(workspace=ws, user_id=owner.id)

    assert WorkspaceMembership in calls
