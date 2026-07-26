"""Tenancy service helpers: the default workspace + domain auto-join — the glue
that makes scoping non-breaking (existing + new domain users keep access)."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.workspaces import services
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def test_ensure_default_workspace_is_idempotent_and_domain_seeded(settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com,dimagi-ai.com"
    su = User.objects.create(username="su", email="su@dimagi.com", is_superuser=True)
    ws = services.ensure_default_workspace()
    assert ws is not None
    assert ws.slug == services.DEFAULT_WORKSPACE_SLUG
    assert ws.created_by == su
    assert ws.auto_join_domains == ["dimagi.com", "dimagi-ai.com"]
    # owner is an owner-member; idempotent
    assert WorkspaceMembership.objects.get(workspace=ws, user=su).role == "owner"
    assert services.ensure_default_workspace().pk == ws.pk


def test_ensure_default_workspace_none_without_users():
    assert services.ensure_default_workspace() is None


def test_auto_join_adds_matching_domain_user_only(settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"
    User.objects.create(username="su", email="su@dimagi.com", is_superuser=True)
    services.ensure_default_workspace()

    insider = User.objects.create(username="i", email="i@dimagi.com")
    services.auto_join_workspaces(insider)
    assert WorkspaceMembership.objects.get(
        workspace_id=services.DEFAULT_WORKSPACE_SLUG, user=insider
    ).role == "editor"

    outsider = User.objects.create(username="o", email="o@other.com")
    services.auto_join_workspaces(outsider)
    assert not WorkspaceMembership.objects.filter(user=outsider).exists()


def test_user_workspace_slugs_and_is_member(settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"
    su = User.objects.create(username="su", email="su@dimagi.com", is_superuser=True)
    services.ensure_default_workspace()
    assert services.user_workspace_slugs(su) == {services.DEFAULT_WORKSPACE_SLUG}
    assert services.is_member(su, services.DEFAULT_WORKSPACE_SLUG) is True
    other = User.objects.create(username="x", email="x@other.com")
    assert services.user_workspace_slugs(other) == set()
    assert services.is_member(other, services.DEFAULT_WORKSPACE_SLUG) is False


# ──────────────────────────────────────────────────────────────────────
# F1 (2026-07-26 security review): `ensure_member` returns (membership,
# created) and can record provenance via `provisioned_by_app`, so an
# app-provisioned grant is attributable and findable — an organic join
# (no app credential involved) must record no provenance.
# ──────────────────────────────────────────────────────────────────────


def test_ensure_member_returns_membership_and_created_flag():
    su = User.objects.create(username="su2", email="su2@dimagi.com")
    ws = Workspace.objects.create(slug="ws1", display_name="WS1", created_by=su)
    user = User.objects.create(username="m1", email="m1@dimagi.com")

    m, created = services.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    assert created is True
    assert m.role == WorkspaceMembership.EDITOR

    m2, created2 = services.ensure_member(ws, user, WorkspaceMembership.VIEWER)
    assert created2 is False
    assert m2.pk == m.pk
    assert m2.role == WorkspaceMembership.EDITOR  # unchanged — create-only


def test_ensure_member_records_provisioning_app_on_create_only():
    from apps.tokens.models import AppCredential

    su = User.objects.create(username="su3", email="su3@dimagi.com")
    ws = Workspace.objects.create(slug="ws2", display_name="WS2", created_by=su)
    _, cred = AppCredential.create_credential(name="prov-app", domains=["dimagi.com"], created_by=su)

    user = User.objects.create(username="m2", email="m2@dimagi.com")
    m, created = services.ensure_member(
        ws, user, WorkspaceMembership.EDITOR, provisioned_by_app=cred,
    )
    assert created is True
    assert m.provisioned_by_app_id == cred.pk


def test_ensure_member_organic_join_has_no_provisioning_app():
    su = User.objects.create(username="su4", email="su4@dimagi.com")
    ws = Workspace.objects.create(slug="ws3", display_name="WS3", created_by=su)
    user = User.objects.create(username="m3", email="m3@dimagi.com")

    m, _created = services.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    assert m.provisioned_by_app_id is None


def test_provisioned_membership_protects_its_credential_from_deletion(db):
    """The provenance FK is an audit field: deleting an AppCredential must not
    silently erase the trail on the memberships it created. Retire a credential
    with `revoked_at` (row kept, trail intact) — deletion is blocked while it
    still has provisioned memberships to answer for."""
    from django.contrib.auth import get_user_model
    from django.db.models import ProtectedError

    from apps.tokens.models import AppCredential
    from apps.workspaces.models import Workspace, WorkspaceMembership
    from apps.workspaces.services import ensure_member

    User = get_user_model()
    admin = User.objects.create_user("prov-admin", "prov-admin@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="prov-ws", display_name="Prov", created_by=admin)
    _raw, cred = AppCredential.create_credential(
        name="prov-app", domains=["dimagi.com"], created_by=admin
    )
    user = User.objects.create_user("prov-u", "prov-u@dimagi.com", "pw")
    ensure_member(ws, user, WorkspaceMembership.EDITOR, provisioned_by_app=cred)

    with pytest.raises(ProtectedError):
        cred.delete()

    # Revoking is the supported retirement path and keeps the trail readable.
    cred.revoked_at = timezone.now()
    cred.save(update_fields=["revoked_at"])
    m = WorkspaceMembership.objects.get(workspace=ws, user=user)
    assert m.provisioned_by_app_id == cred.pk
