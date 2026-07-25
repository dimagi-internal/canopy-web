"""Workspaces REST API — create/list/get with membership-scoped RBAC.
A workspace is visible only to its members; a non-member gets 404 (no existence
leak), mirroring the tokenless-visibility discipline elsewhere."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(email):
    return User.objects.create(username=email, email=email)


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


def test_list_requires_auth():
    assert Client().get("/api/workspaces/").status_code in (401, 403)


def test_create_makes_creator_an_owner():
    u = _user("a@dimagi.com")
    r = _post(_client(u), "/api/workspaces/", {"slug": "acme", "display_name": "Acme"})
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["slug"] == "acme"
    assert body["role"] == "owner"
    # auto_join_domains is server-only — never settable from the request.
    assert body["auto_join_domains"] == []
    assert WorkspaceMembership.objects.get(workspace_id="acme", user=u).role == "owner"


def test_create_rejects_caller_supplied_auto_join_domains():
    """F1: auto_join_domains grants domain-wide standing (every user of that
    domain auto-joins as editor) — it must never be client input. Sending it
    at all is rejected (422), not silently ignored, so a caller can't be
    fooled into thinking it took effect."""
    u = _user("a@dimagi.com")
    r = _post(_client(u), "/api/workspaces/", {
        "slug": "acme", "display_name": "Acme", "auto_join_domains": ["dimagi.com"],
    })
    assert r.status_code == 422, r.content
    assert not Workspace.objects.filter(slug="acme").exists()


def test_create_rejected_for_invite_admitted_membership_less_user():
    """F1: an invite-admitted external user with NO workspace standing yet
    must not be able to bootstrap their own workspace (which would let them
    mint invites of their own, transitively re-admitting arbitrary emails
    past the login gate)."""
    outsider = _user("outsider@external.com")
    r = _post(_client(outsider), "/api/workspaces/", {"slug": "evil", "display_name": "Evil"})
    assert r.status_code == 403, r.content
    assert not Workspace.objects.filter(slug="evil").exists()


def test_create_allowed_for_non_allowlisted_user_who_already_has_membership():
    """F1's gate is create-only, not membership-wide: once an external user
    has genuine standing (an existing WorkspaceMembership, e.g. from
    accepting an invite), they may create workspaces like anyone else."""
    owner = _user("owner@dimagi.com")
    existing_ws = Workspace.objects.create(slug="acme", display_name="Acme", created_by=owner)
    member = _user("member@external.com")
    WorkspaceMembership.objects.create(workspace=existing_ws, user=member, role="editor")

    r = _post(_client(member), "/api/workspaces/", {"slug": "second", "display_name": "Second"})
    assert r.status_code == 201, r.content


def test_list_is_member_scoped():
    a, b = _user("a@dimagi.com"), _user("b@dimagi.com")
    _post(_client(a), "/api/workspaces/", {"slug": "acme", "display_name": "Acme"})
    _post(_client(b), "/api/workspaces/", {"slug": "beta", "display_name": "Beta"})
    a_slugs = {w["slug"] for w in _client(a).get("/api/workspaces/").json()}
    assert a_slugs == {"acme"}


def test_get_is_member_only_else_404():
    a, b = _user("a@dimagi.com"), _user("b@dimagi.com")
    _post(_client(a), "/api/workspaces/", {"slug": "acme", "display_name": "Acme"})
    assert _client(a).get("/api/workspaces/acme/").json()["role"] == "owner"
    # a non-member can't even tell it exists
    assert _client(b).get("/api/workspaces/acme/").status_code == 404


def test_duplicate_slug_conflicts():
    a = _user("a@dimagi.com")
    _post(_client(a), "/api/workspaces/", {"slug": "acme", "display_name": "Acme"})
    dup = _post(_client(a), "/api/workspaces/", {"slug": "acme", "display_name": "Dup"})
    assert dup.status_code == 409
