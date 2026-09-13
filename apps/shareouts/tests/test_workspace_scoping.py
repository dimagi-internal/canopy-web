"""End-to-end workspace scoping of the live /api/shareouts surface.

POST with no workspace → default workspace + creator membership (so the machine
producers that post shareouts keep working); a domain teammate who has NOT
explicitly joined (2026-09-12: auto-join is gone — self-join replaces it, see
docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md) gets
an empty list same as an outsider; and the prefixed /api/w/{ws}/shareouts/
route works for a member.

Modeled on apps/agents/tests/test_workspace_scoping.py.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.shareouts.models import Shareout
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


def _item(**overrides):
    # Roll-up rows (project_slug=None) so scoping tests don't need a Project.
    base = {
        "project_slug": None,
        "period_start": "2026-06-03T09:00:00Z",
        "period_end": "2026-06-03T17:30:00Z",
        "title": "Weekly briefing",
        "summary": "TL;DR",
        "content": "## What\nShipped things.",
        "author": "jj",
        "source": "canopy:shareout@2026-06-04T00:00:00",
    }
    base.update(overrides)
    return base


def _post_shareout(client, url="/api/shareouts/", **overrides):
    return client.post(
        url,
        data=json.dumps({"shareouts": [_item(**overrides)]}),
        content_type="application/json",
    )


def test_create_without_workspace_assigns_default_and_keeps_creator_in():
    jj = _user("jj@dimagi.com", is_superuser=True)  # the human/PAT that posts
    assert _post_shareout(_client(jj)).status_code == 201
    row = Shareout.objects.get()
    assert row.workspace_id == DEFAULT_WORKSPACE_SLUG
    # The creator still sees what they just posted on the flat mount.
    items = _client(jj).get("/api/shareouts/").json()["items"]
    assert any(i["title"] == "Weekly briefing" for i in items)


def test_domain_teammate_no_longer_auto_joins_gets_empty_list():
    """Rewrite of `test_domain_teammate_auto_joins_and_sees_shareout`: that
    test asserted auto-join behaviour that no longer exists. A same-domain
    teammate who has never explicitly joined is a non-member like any
    other."""
    jj = _user("jj@dimagi.com", is_superuser=True)
    _post_shareout(_client(jj))
    teammate = _user("t@dimagi.com")  # never explicitly added
    items = _client(teammate).get("/api/shareouts/").json()["items"]
    assert all(i["title"] != "Weekly briefing" for i in items)
    from apps.workspaces.models import WorkspaceMembership

    assert not WorkspaceMembership.objects.filter(user=teammate).exists()


def test_outsider_gets_empty_list():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _post_shareout(_client(jj))
    outsider = _user("x@other.com")
    items = _client(outsider).get("/api/shareouts/").json()["items"]
    assert items == []


def test_prefixed_workspace_route_works_for_member():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _post_shareout(_client(jj))  # creates + joins the default workspace
    resp = _client(jj).get(f"/api/w/{DEFAULT_WORKSPACE_SLUG}/shareouts/")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(i["title"] == "Weekly briefing" for i in items)


def _workspace(slug):
    from apps.workspaces.models import Workspace
    owner = _user(f"owner-{slug}@{slug}.example")
    ws, _ = Workspace.objects.get_or_create(
        slug=slug, defaults={"display_name": slug, "created_by": owner, "self_join_domains": []}
    )
    return ws


def _shareout_in(ws):
    return Shareout.objects.create(
        project=None, workspace=ws,
        period_start="2026-06-03T09:00:00Z", period_end="2026-06-03T17:30:00Z",
        title="Other tenant's briefing", content="secret", source="s",
    )


def test_clear_with_no_filters_never_deletes_another_workspaces_shareouts():
    # A dimagi-only user clearing "everything" must not wipe connect's shareouts —
    # the empty-body clear is scoped to the caller's own workspaces.
    connect_row = _shareout_in(_workspace("connect"))
    jj = _user("jj@dimagi.com", is_superuser=True)
    resp = _client(jj).post(
        "/api/shareouts/clear/", data=json.dumps({}), content_type="application/json"
    )
    assert resp.status_code == 200
    assert Shareout.objects.filter(pk=connect_row.pk).exists()  # survived the cross-tenant clear
