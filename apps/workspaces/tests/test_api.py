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
    # self_join_domains is server-only — never settable from the request.
    assert body["self_join_domains"] == []
    assert WorkspaceMembership.objects.get(workspace_id="acme", user=u).role == "owner"


def test_create_rejects_caller_supplied_self_join_domains():
    """F1: self_join_domains grants domain-wide standing (every user of that
    domain may self-join as editor) — it must never be client input. Sending
    it at all is rejected (422), not silently ignored, so a caller can't be
    fooled into thinking it took effect."""
    u = _user("a@dimagi.com")
    r = _post(_client(u), "/api/workspaces/", {
        "slug": "acme", "display_name": "Acme", "self_join_domains": ["dimagi.com"],
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


# ---- self-join (replaces the old implicit auto-join, 2026-09-12) ----


def test_joinable_lists_matching_workspace_and_omits_others(settings):
    """Lists a workspace whose self_join_domains matches the caller, omits
    one whose domains don't match, and omits one the caller already belongs
    to — a capability list, not a directory."""
    owner = _user("owner@dimagi.com")
    matching = Workspace.objects.create(
        slug="dimagi-team", display_name="Dimagi Team", created_by=owner,
        self_join_domains=["dimagi.com"],
    )
    Workspace.objects.create(
        slug="acme-team", display_name="Acme Team", created_by=owner,
        self_join_domains=["acme.com"],
    )
    already_member = Workspace.objects.create(
        slug="already", display_name="Already", created_by=owner,
        self_join_domains=["dimagi.com"],
    )
    caller = _user("caller@dimagi.com")
    WorkspaceMembership.objects.create(
        workspace=already_member, user=caller, role=WorkspaceMembership.VIEWER,
    )

    body = _client(caller).get("/api/workspaces/joinable").json()
    slugs = {row["slug"] for row in body}
    assert slugs == {matching.slug}
    assert body[0]["domain"] == "dimagi.com"


def test_join_creates_editor_membership_and_is_idempotent():
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(
        slug="dimagi-team", display_name="Dimagi Team", created_by=owner,
        self_join_domains=["dimagi.com"],
    )
    caller = _user("caller@dimagi.com")

    r = _post(_client(caller), f"/api/workspaces/{ws.slug}/join", {})
    assert r.status_code == 200, r.content
    assert r.json()["role"] == "editor"
    assert WorkspaceMembership.objects.get(workspace=ws, user=caller).role == "editor"

    # Second call is a no-op, not an error and not a second row.
    r2 = _post(_client(caller), f"/api/workspaces/{ws.slug}/join", {})
    assert r2.status_code == 200, r2.content
    assert r2.json()["role"] == "editor"
    assert WorkspaceMembership.objects.filter(workspace=ws, user=caller).count() == 1


def test_join_cannot_be_used_to_elevate_an_existing_viewer():
    """The obvious attack on a self-service join endpoint: an existing
    `viewer` calling `join` must stay a `viewer`, never be promoted to
    `editor`. `ensure_member` is create-only, so this must hold."""
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(
        slug="dimagi-team", display_name="Dimagi Team", created_by=owner,
        self_join_domains=["dimagi.com"],
    )
    viewer = _user("viewer@dimagi.com")
    WorkspaceMembership.objects.create(
        workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER,
    )

    r = _post(_client(viewer), f"/api/workspaces/{ws.slug}/join", {})
    assert r.status_code == 200, r.content
    assert r.json()["role"] == "viewer"
    assert WorkspaceMembership.objects.get(workspace=ws, user=viewer).role == "viewer"


def test_join_nonmatching_domain_gets_404_not_403():
    """A 403 here would tell any signed-in user the workspace exists and
    which domains it trusts — a tenant-enumeration oracle. Must be the SAME
    404 a nonexistent slug returns."""
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(
        slug="acme-team", display_name="Acme Team", created_by=owner,
        self_join_domains=["acme.com"],
    )
    outsider = _user("outsider@dimagi.com")

    r = _post(_client(outsider), f"/api/workspaces/{ws.slug}/join", {})
    assert r.status_code == 404, r.content
    assert not WorkspaceMembership.objects.filter(workspace=ws, user=outsider).exists()


def test_join_nonexistent_slug_gets_the_same_404():
    caller = _user("caller@dimagi.com")
    r_missing = _post(_client(caller), "/api/workspaces/does-not-exist/join", {})
    owner = _user("owner@dimagi.com")
    ws = Workspace.objects.create(
        slug="acme-team", display_name="Acme Team", created_by=owner,
        self_join_domains=["acme.com"],
    )
    r_mismatch = _post(_client(caller), f"/api/workspaces/{ws.slug}/join", {})
    # Same STATUS (404, never 403 on the mismatch case) and the same RFC 7807
    # `type`/`title` shape for both — the bodies differ only in which slug
    # they echo back, not in whether one leaks more than the other.
    assert r_missing.status_code == r_mismatch.status_code == 404
    assert r_missing.json()["type"] == r_mismatch.json()["type"]
    assert r_missing.json()["title"] == "workspace 'does-not-exist' not found"
    assert r_mismatch.json()["title"] == f"workspace '{ws.slug}' not found"
