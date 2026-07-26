"""Tests for `manage.py create_app_credential --workspace/--role` (Task 1,
Step 4 of docs/superpowers/plans/2026-07-26-tenant-scoped-provisioning.md)."""
from __future__ import annotations

import io

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


@pytest.mark.django_db
def test_command_without_workspace_grants_no_provisioning():
    out = io.StringIO()
    call_command("create_app_credential", "--name", "plain-app", "--domains", "dimagi.com", stdout=out)
    cred = AppCredential.objects.get(name="plain-app")
    assert cred.provision_workspace_id is None


@pytest.mark.django_db
def test_command_with_workspace_and_role_grants_provisioning():
    user = User.objects.create_user("cmdadmin", "cmdadmin@dimagi.com", "pw")
    Workspace.objects.create(slug="connect", display_name="Connect", created_by=user)
    out = io.StringIO()
    call_command(
        "create_app_credential", "--name", "connect-app", "--domains", "dimagi.com",
        "--workspace", "connect", "--role", "viewer", stdout=out,
    )
    cred = AppCredential.objects.get(name="connect-app")
    assert cred.provision_workspace_id == "connect"
    assert cred.provision_role == WorkspaceMembership.VIEWER
    assert "connect" in out.getvalue()


@pytest.mark.django_db
def test_command_rejects_owner_role():
    user = User.objects.create_user("cmdadmin2", "cmdadmin2@dimagi.com", "pw")
    Workspace.objects.create(slug="connect2", display_name="Connect2", created_by=user)
    with pytest.raises(CommandError):
        call_command(
            "create_app_credential", "--name", "owner-app", "--domains", "dimagi.com",
            "--workspace", "connect2", "--role", "owner",
        )
    assert not AppCredential.objects.filter(name="owner-app").exists()


@pytest.mark.django_db
def test_command_rejects_unknown_workspace():
    with pytest.raises(CommandError):
        call_command(
            "create_app_credential", "--name", "ghost-app", "--domains", "dimagi.com",
            "--workspace", "does-not-exist",
        )
    assert not AppCredential.objects.filter(name="ghost-app").exists()
