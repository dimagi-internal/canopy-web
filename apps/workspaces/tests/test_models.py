"""Workspace tenancy models — ported from ace-web apps/workspaces, domain-agnostic
(no Drive folder). The unit of multi-tenancy: a Workspace owns members (roles) and
pending email invites."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from apps.workspaces.models import Workspace, WorkspaceInvite, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email="a@dimagi.com"):
    return User.objects.create(username=email, email=email)


def test_workspace_holds_members_and_settings():
    u = _user()
    ws = Workspace.objects.create(
        slug="acme", display_name="Acme", created_by=u, self_join_domains=["acme.com"]
    )
    m = WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    assert ws.memberships.count() == 1
    assert m.role == "owner"
    assert ws.self_join_domains == ["acme.com"]
    assert ws.settings == {}
    # the ace-specific Drive coupling is dropped in the framework port
    assert not hasattr(ws, "drive_root_folder_id")


def test_membership_is_unique_per_user_per_workspace():
    u = _user()
    ws = Workspace.objects.create(slug="x", display_name="X", created_by=u)
    WorkspaceMembership.objects.create(workspace=ws, user=u, role="owner")
    with pytest.raises(IntegrityError):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role="viewer")


def test_invite_token_and_pending_lifecycle():
    u = _user()
    ws = Workspace.objects.create(slug="x", display_name="X", created_by=u)
    inv = WorkspaceInvite.objects.create(
        workspace=ws, email="b@x.com", invited_by=u,
        expires_at=timezone.now() + dt.timedelta(days=7),
    )
    assert inv.role == "editor"            # default
    assert len(inv.token) >= 40            # auto-generated url-safe token
    assert inv.is_pending() is True
    # revoking ends the pending window
    inv.revoked_at = timezone.now()
    inv.save()
    assert inv.is_pending() is False


def test_invite_not_pending_when_expired():
    u = _user()
    ws = Workspace.objects.create(slug="x", display_name="X", created_by=u)
    inv = WorkspaceInvite.objects.create(
        workspace=ws, email="b@x.com", invited_by=u,
        expires_at=timezone.now() - dt.timedelta(minutes=1),
    )
    assert inv.is_pending() is False
