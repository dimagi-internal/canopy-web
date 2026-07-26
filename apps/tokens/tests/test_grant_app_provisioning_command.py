"""Tests for `manage.py grant_app_provisioning` (F5, 2026-07-26 security
review) — grants/changes provisioning on an EXISTING credential, the
production-cutover path `create_app_credential` cannot cover since it
hard-fails on a duplicate --name."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


@pytest.mark.django_db
def test_grant_sets_provisioning_on_a_credential_with_none():
    admin = User.objects.create_user("gadmin", "gadmin@dimagi.com", "pw")
    Workspace.objects.create(slug="connect", display_name="Connect", created_by=admin)
    _, cred = AppCredential.create_credential(name="ace-web", domains=["dimagi.com"], created_by=admin)
    assert cred.provision_workspace_id is None

    call_command("grant_app_provisioning", "--name", "ace-web", "--workspace", "connect", "--role", "editor")

    cred.refresh_from_db()
    assert cred.provision_workspace_id == "connect"
    assert cred.provision_role == WorkspaceMembership.EDITOR


@pytest.mark.django_db
def test_grant_changes_an_already_granted_credential():
    admin = User.objects.create_user("gadmin2", "gadmin2@dimagi.com", "pw")
    Workspace.objects.create(slug="ws-a", display_name="A", created_by=admin)
    ws_b = Workspace.objects.create(slug="ws-b", display_name="B", created_by=admin)
    _, cred = AppCredential.create_credential(
        name="switchable", domains=["dimagi.com"], created_by=admin,
        provision_workspace=Workspace.objects.get(slug="ws-a"),
        provision_role=WorkspaceMembership.VIEWER,
    )

    call_command("grant_app_provisioning", "--name", "switchable", "--workspace", "ws-b", "--role", "editor")

    cred.refresh_from_db()
    assert cred.provision_workspace_id == ws_b.slug
    assert cred.provision_role == WorkspaceMembership.EDITOR


@pytest.mark.django_db
def test_grant_defaults_role_to_editor():
    admin = User.objects.create_user("gadmin3", "gadmin3@dimagi.com", "pw")
    Workspace.objects.create(slug="ws-c", display_name="C", created_by=admin)
    _, cred = AppCredential.create_credential(name="default-role", domains=["dimagi.com"], created_by=admin)

    call_command("grant_app_provisioning", "--name", "default-role", "--workspace", "ws-c")

    cred.refresh_from_db()
    assert cred.provision_role == WorkspaceMembership.EDITOR


@pytest.mark.django_db
def test_grant_rejects_owner_role():
    admin = User.objects.create_user("gadmin4", "gadmin4@dimagi.com", "pw")
    Workspace.objects.create(slug="ws-d", display_name="D", created_by=admin)
    _, cred = AppCredential.create_credential(name="owner-attempt", domains=["dimagi.com"], created_by=admin)

    with pytest.raises(CommandError):
        call_command("grant_app_provisioning", "--name", "owner-attempt", "--workspace", "ws-d", "--role", "owner")

    cred.refresh_from_db()
    assert cred.provision_workspace_id is None  # unchanged


@pytest.mark.django_db
def test_grant_rejects_unknown_credential():
    with pytest.raises(CommandError):
        call_command("grant_app_provisioning", "--name", "does-not-exist", "--workspace", "connect")


@pytest.mark.django_db
def test_grant_rejects_unknown_workspace():
    admin = User.objects.create_user("gadmin5", "gadmin5@dimagi.com", "pw")
    _, cred = AppCredential.create_credential(name="ws-ghost", domains=["dimagi.com"], created_by=admin)

    with pytest.raises(CommandError):
        call_command("grant_app_provisioning", "--name", "ws-ghost", "--workspace", "does-not-exist")

    cred.refresh_from_db()
    assert cred.provision_workspace_id is None  # unchanged
