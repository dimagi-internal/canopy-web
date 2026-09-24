"""Disconnecting a site has to disconnect it now, not within the hour.

Revoking an `AppCredential` stopped it minting anything new and 404'd its embed
shell — but every delegated token it had ALREADY minted kept authenticating
until it expired, up to a full hour. An open widget carried on reading and
writing as the user for that whole window.

The gap was hidden by the one endpoint that did check: `/api/embed/agents`
re-reads `app.revoked_at` in `_acting_app`, so the surface most likely to be
spot-checked behaved correctly while every other one did not.

Revocation is reached for when a secret has leaked. A control that takes an
hour is not the control the button promises.
"""

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.tokens.models import AppCredential, DelegatedToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _live_token():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    _raw, app = AppCredential.create_credential(name="x", created_by=user, workspace=ws)
    token, _row = DelegatedToken.issue(app=app, user=user, ttl_seconds=3600)
    return user, app, token


def _revoke(app):
    app.revoked_at = timezone.now()
    app.save(update_fields=["revoked_at"])


def test_a_live_token_stops_reading_the_moment_its_app_is_revoked():
    _user, app, token = _live_token()
    assert Client().get(
        "/api/canopy-sessions/", HTTP_AUTHORIZATION=f"Bearer {token}"
    ).status_code == 200

    _revoke(app)

    r = Client().get("/api/canopy-sessions/", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 401, "a revoked app's token still authenticates"


def test_and_stops_writing():
    """The read being fixed is not enough — a leaked secret's danger is what it
    can change."""
    _user, app, token = _live_token()
    _revoke(app)

    r = Client().post(
        "/api/canopy-sessions/",
        data={"agent_slug": None, "title": "x", "metadata": {}},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )

    assert r.status_code == 401


def test_the_lookup_itself_refuses():
    """Pinned at the resolver rather than only through a route, because every
    bearer-authenticated surface in the app depends on this one function."""
    _user, app, token = _live_token()
    assert DelegatedToken.lookup(token) is not None
    _revoke(app)
    assert DelegatedToken.lookup(token) is None


def test_an_unrevoked_app_is_unaffected():
    _user, _app, token = _live_token()
    assert DelegatedToken.lookup(token) is not None


def test_expiry_still_applies_on_its_own():
    """Two independent reasons to refuse; neither replaces the other."""
    user, app, _token = _live_token()
    raw, _row = DelegatedToken.issue(app=app, user=user, ttl_seconds=-1)
    assert DelegatedToken.lookup(raw) is None
