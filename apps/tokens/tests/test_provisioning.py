"""Model-level guarantees for AppCredential tenant provisioning
(`provision_workspace` / `provision_role`).

Companion to `tests/test_token_exchange.py`, which covers the exchange-time
behavior (create-only membership, gate ordering). These tests cover the
model/classmethod-level "owner is never mintable" guarantee — both through
the intended `create_credential` entry point and by bypassing it, since a
shell caller (`AppCredential.objects.create(...)`) must be blocked too.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


@pytest.mark.django_db
def test_create_credential_rejects_owner_provision_role():
    user = User.objects.create_user("admin3", "admin3@dimagi.com", "pw")
    with pytest.raises(ValueError):
        AppCredential.create_credential(
            name="bad-owner-cred",
            domains=["dimagi.com"],
            created_by=user,
            provision_role=WorkspaceMembership.OWNER,
        )
    # The reject must be atomic — no half-created row left behind.
    assert not AppCredential.objects.filter(name="bad-owner-cred").exists()


@pytest.mark.django_db
def test_model_level_guard_rejects_owner_provision_role_bypassing_classmethod():
    """A shell caller using the model manager directly (not create_credential)
    must still be blocked — the no-owner rule lives at the model level too."""
    user = User.objects.create_user("admin4", "admin4@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="guardws", display_name="Guard WS", created_by=user)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AppCredential.objects.create(
                name="direct-owner-cred",
                token_hash="deadbeef" * 8,
                allowed_delegation_domains=[],
                created_by=user,
                provision_workspace=ws,
                provision_role=WorkspaceMembership.OWNER,
            )
    assert not AppCredential.objects.filter(name="direct-owner-cred").exists()


@pytest.mark.django_db
def test_create_credential_defaults_to_editor_and_no_workspace():
    user = User.objects.create_user("admin5", "admin5@dimagi.com", "pw")
    _, cred = AppCredential.create_credential(name="plain", domains=["dimagi.com"], created_by=user)
    assert cred.provision_workspace_id is None
    assert cred.provision_role == WorkspaceMembership.EDITOR


@pytest.mark.django_db
def test_create_credential_can_grant_a_workspace_and_role():
    user = User.objects.create_user("admin6", "admin6@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="granted", display_name="Granted", created_by=user)
    _, cred = AppCredential.create_credential(
        name="granted-cred", domains=["dimagi.com"], created_by=user,
        provision_workspace=ws, provision_role=WorkspaceMembership.VIEWER,
    )
    assert cred.provision_workspace_id == "granted"
    assert cred.provision_role == WorkspaceMembership.VIEWER
