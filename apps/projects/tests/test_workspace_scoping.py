"""End-to-end workspace scoping of the live /api/projects surface.

create with no workspace → default workspace + creator membership (so an
unchanged flat client keeps working); a domain teammate who has NOT
explicitly joined (2026-09-12: auto-join is gone — self-join replaces it,
see docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md)
gets the same 404 and empty list an outsider does. Also verifies the
prefixed /api/w/{ws}/projects/ mount works for a member.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.projects.models import Project
from apps.workspaces.services import DEFAULT_WORKSPACE_SLUG

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"


def _user(email, **kw):
    return User.objects.create(username=email, email=email, **kw)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


def _create_project(client, slug="canopy-web", url="/api/projects/"):
    return _post(client, url, {"name": "Canopy Web", "slug": slug})


def test_create_without_workspace_assigns_default_and_keeps_creator_in():
    jj = _user("jj@dimagi.com", is_superuser=True)  # the human who owns the org
    assert _create_project(_client(jj)).status_code == 201
    project = Project.objects.get(slug="canopy-web")
    assert project.workspace_id == DEFAULT_WORKSPACE_SLUG
    # The creator keeps access to detail + nested surfaces.
    assert _client(jj).get("/api/projects/canopy-web/").status_code == 200
    assert _client(jj).get("/api/projects/canopy-web/context/").status_code == 200
    assert _client(jj).get("/api/projects/canopy-web/actions/").status_code == 200


def test_domain_teammate_no_longer_auto_joins_gets_404_and_empty_list():
    """Rewrite of `test_domain_teammate_auto_joins_and_sees_project`: that
    test asserted auto-join behaviour that no longer exists. A same-domain
    teammate who has never explicitly joined is a non-member like any
    other."""
    jj = _user("jj@dimagi.com", is_superuser=True)
    _create_project(_client(jj))
    teammate = _user("t@dimagi.com")  # never explicitly added
    assert _client(teammate).get("/api/projects/canopy-web/").status_code == 404
    items = _client(teammate).get("/api/projects/").json()["items"]
    assert all(p["slug"] != "canopy-web" for p in items)
    from apps.workspaces.models import WorkspaceMembership

    assert not WorkspaceMembership.objects.filter(user=teammate).exists()


def test_outsider_gets_404_and_empty_list():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _create_project(_client(jj))
    outsider = _user("x@other.com")
    assert _client(outsider).get("/api/projects/canopy-web/").status_code == 404
    items = _client(outsider).get("/api/projects/").json()["items"]
    assert all(p["slug"] != "canopy-web" for p in items)


def test_outsider_cannot_touch_nested_surfaces():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _create_project(_client(jj))
    outsider = _user("x@other.com")
    c = _client(outsider)
    assert c.get("/api/projects/canopy-web/context/").status_code == 404
    assert c.get("/api/projects/canopy-web/actions/").status_code == 404
    assert c.delete("/api/projects/canopy-web/").status_code == 404


def test_prefixed_workspace_mount_works_for_member():
    jj = _user("jj@dimagi.com", is_superuser=True)
    # Seed via the flat mount so the default workspace ("dimagi") exists.
    assert _create_project(_client(jj)).status_code == 201
    # The prefixed mount routes to the flat surface for a member of "dimagi".
    resp = _client(jj).get(f"/api/w/{DEFAULT_WORKSPACE_SLUG}/projects/")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(p["slug"] == "canopy-web" for p in items)


def test_prefixed_workspace_mount_404s_for_non_member():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _create_project(_client(jj))
    outsider = _user("x@other.com")
    # Non-member of "dimagi" is rejected by the tenancy middleware.
    resp = _client(outsider).get(f"/api/w/{DEFAULT_WORKSPACE_SLUG}/projects/")
    assert resp.status_code == 404
