"""Pre-auth invite preview: `GET /api/workspaces/invites/{token}/preview`.

A visitor arriving at /invite/:token (Task 5) is typically NOT logged in yet.
This endpoint lets them see what they were invited to before being sent
through Google OAuth. Minimal disclosure is the point — invite tokens get
forwarded, pasted into Slack, etc. — so a NON-pending invite reveals only its
status, never the workspace it points at, and the email hint is masked.

Covers both the service-layer helpers (`invite_status`, `mask_email`) and the
`auth=None` API route end to end.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.workspaces.models import Workspace
from apps.workspaces.services import (
    accept_invite,
    create_invite,
    invite_status,
    mask_email,
    revoke_invite,
)

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _ws(owner, slug="acme"):
    return Workspace.objects.create(slug=slug, display_name="Acme Corp", created_by=owner)


# ---- mask_email ----


def test_mask_email_never_reveals_full_local_part_for_long_local():
    assert mask_email("jonathan@dimagi.com") == "j•••@dimagi.com"


def test_mask_email_handles_one_char_local_part_without_leaking_it():
    masked = mask_email("b@dimagi.com")
    assert masked.startswith("b")
    assert masked.endswith("@dimagi.com")
    assert masked != "b@dimagi.com"
    # never bare-exposes just the one character as the whole local part
    assert masked == "b•••@dimagi.com"


def test_mask_email_handles_two_char_local_part_without_leaking_it():
    masked = mask_email("bo@dimagi.com")
    assert masked.startswith("b")
    assert "bo" not in masked
    assert masked == "b•••@dimagi.com"


# ---- invite_status ----


def test_invite_status_pending():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    assert invite_status(inv) == "pending"


def test_invite_status_expired():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    inv.expires_at = timezone.now() - dt.timedelta(days=1)
    inv.save(update_fields=["expires_at"])
    assert invite_status(inv) == "expired"


def test_invite_status_revoked():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=inv)
    assert invite_status(inv) == "revoked"


def test_invite_status_accepted():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    accept_invite(token=inv.token, user=_user("b@dimagi.com"))
    inv.refresh_from_db()
    assert invite_status(inv) == "accepted"


# ---- GET /api/workspaces/invites/{token}/preview ----


def test_preview_pending_invite_returns_full_details_anonymously():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="jonathan@dimagi.com", role="editor", invited_by=owner)

    r = Client().get(f"/api/workspaces/invites/{inv.token}/preview")

    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "pending"
    assert body["workspace_slug"] == "acme"
    assert body["workspace_display_name"] == "Acme Corp"
    assert body["role"] == "editor"
    assert body["email_hint"] == "j•••@dimagi.com"


def test_preview_expired_invite_returns_status_without_workspace_details():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    inv.expires_at = timezone.now() - dt.timedelta(days=1)
    inv.save(update_fields=["expires_at"])

    r = Client().get(f"/api/workspaces/invites/{inv.token}/preview")

    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "expired"
    assert body.get("workspace_slug") is None
    assert body.get("workspace_display_name") is None
    assert body.get("role") is None


def test_preview_revoked_invite_returns_status_without_workspace_details():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    revoke_invite(invite=inv)

    r = Client().get(f"/api/workspaces/invites/{inv.token}/preview")

    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "revoked"
    assert body.get("workspace_slug") is None
    assert body.get("workspace_display_name") is None
    assert body.get("role") is None


def test_preview_accepted_invite_returns_status_without_workspace_details():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)
    accept_invite(token=inv.token, user=_user("b@dimagi.com"))

    r = Client().get(f"/api/workspaces/invites/{inv.token}/preview")

    assert r.status_code == 200, r.content
    body = r.json()
    assert body["status"] == "accepted"
    assert body.get("workspace_slug") is None
    assert body.get("workspace_display_name") is None
    assert body.get("role") is None


def test_preview_unknown_token_404s():
    r = Client().get("/api/workspaces/invites/does-not-exist/preview")
    assert r.status_code == 404


def test_preview_masks_short_local_part_email_end_to_end():
    owner = _user("a@dimagi.com")
    ws = _ws(owner)
    inv = create_invite(workspace=ws, email="b@dimagi.com", role="editor", invited_by=owner)

    r = Client().get(f"/api/workspaces/invites/{inv.token}/preview")

    assert r.status_code == 200, r.content
    hint = r.json()["email_hint"]
    assert hint != "b@dimagi.com"
    assert "b@dimagi.com"[0] == hint[0]
