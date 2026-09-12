"""GET /api/me/ reports whether the caller may create a workspace.

The first-run screen needs this to avoid offering a button that 403s. The gate
itself is apps.workspaces.services.can_create_workspace (the F1 finding).
"""
import pytest
from django.contrib.auth import get_user_model

from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()


@pytest.fixture
def allowlisted(db):
    return User.objects.create_user(username="ally", email="ally@dimagi.com")


@pytest.fixture
def invited(db):
    return User.objects.create_user(username="guest", email="guest@example.org")


def test_allowlisted_domain_may_create(client, allowlisted):
    client.force_login(allowlisted)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is True


def test_invite_admitted_with_no_membership_may_not_create(client, invited):
    client.force_login(invited)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is False


def test_invite_admitted_with_a_membership_may_create(client, invited):
    ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=invited)
    WorkspaceMembership.objects.create(
        workspace=ws, user=invited, role=WorkspaceMembership.OWNER
    )
    client.force_login(invited)
    body = client.get("/api/me/").json()
    assert body["can_create_workspace"] is True
