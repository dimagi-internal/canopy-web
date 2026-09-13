"""End-to-end workspace scoping of the /api/reviews surface.

Mirrors apps/agents/tests/test_workspace_scoping.py: create with no workspace →
default workspace + creator membership (so the unchanged orchestrator keeps
working); a domain teammate who has NOT explicitly joined (2026-09-12:
auto-join is gone — self-join replaces it, see
docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md)
does not see it in the list, same as an outsider.

PLUS the visibility invariant that scoping must NOT break: an anonymous caller
can still GET a visibility=link review detail after scoping, and submitting a
decision still requires a login.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.reviews.models import ReviewRequest
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


def _create_review(client, **body):
    payload = {
        "request_json": {"run_id": "demo-2026-06-30-001", "gate": "narrative-agreement", "narrative": "A story"},
        "visibility": "link",
    }
    payload.update(body)
    return client.post("/api/reviews/", data=json.dumps(payload), content_type="application/json")


def test_create_without_workspace_assigns_default_and_keeps_creator_in():
    jj = _user("jj@dimagi.com", is_superuser=True)  # the human who minted the PAT
    resp = _create_review(_client(jj))
    assert resp.status_code == 201
    review = ReviewRequest.objects.get(pk=resp.json()["id"])
    assert review.workspace_id == DEFAULT_WORKSPACE_SLUG
    # The creator still sees it in the dashboard list.
    listing = _client(jj).get("/api/reviews/").json()
    assert any(r["id"] == str(review.id) for r in listing)


def test_domain_teammate_no_longer_auto_joins_gets_empty_list():
    """Rewrite of `test_domain_teammate_auto_joins_and_sees_review`: that
    test asserted auto-join behaviour that no longer exists. A same-domain
    teammate who has never explicitly joined is a non-member like any
    other."""
    jj = _user("jj@dimagi.com", is_superuser=True)
    resp = _create_review(_client(jj))
    rid = resp.json()["id"]
    teammate = _user("t@dimagi.com")  # never explicitly added
    listing = _client(teammate).get("/api/reviews/").json()
    assert all(r["id"] != rid for r in listing)
    from apps.workspaces.models import WorkspaceMembership

    assert not WorkspaceMembership.objects.filter(user=teammate).exists()


def test_outsider_does_not_see_workspace_scoped_review():
    jj = _user("jj@dimagi.com", is_superuser=True)
    resp = _create_review(_client(jj))
    rid = resp.json()["id"]
    outsider = _user("x@other.com")
    listing = _client(outsider).get("/api/reviews/").json()
    assert all(r["id"] != rid for r in listing)


def _workspace(slug):
    from apps.workspaces.models import Workspace
    owner = _user(f"owner-{slug}@{slug}.example")
    ws, _ = Workspace.objects.get_or_create(
        slug=slug,
        defaults={"display_name": slug, "created_by": owner, "self_join_domains": []},  # no self-join domain
    )
    return ws


def _private_review_in(ws):
    return ReviewRequest.objects.create(
        run_id="secret-2026-01-01-001",
        gate="narrative-agreement",
        narrative_slug="secret",
        version=1,
        status=ReviewRequest.STATUS_PENDING,
        request_json={"run_id": "secret-2026-01-01-001", "gate": "narrative-agreement"},
        visibility=ReviewRequest.VISIBILITY_PRIVATE,
        workspace=ws,
    )


@override_settings(REQUIRE_AUTH=True)
def test_non_member_cannot_read_a_private_review_in_another_workspace():
    # jj is a dimagi-domain user (auto-joins the default workspace) but NOT a member
    # of "connect"; a private connect review must be a 404, not a cross-tenant read.
    jj = _user("jj@dimagi.com", is_superuser=True)
    review = _private_review_in(_workspace("connect"))
    resp = _client(jj).get(f"/api/reviews/{review.id}/")
    assert resp.status_code == 404


@override_settings(REQUIRE_AUTH=True)
def test_non_member_cannot_submit_or_delete_another_workspaces_review():
    from apps.workspaces.services import ensure_member
    from apps.workspaces.models import WorkspaceMembership

    jj = _user("jj@dimagi.com", is_superuser=True)
    connect = _workspace("connect")
    review = _private_review_in(connect)

    submit = _client(jj).post(
        f"/api/reviews/{review.id}/submit/",
        data=json.dumps({"response_json": {"decision": "approve"}}),
        content_type="application/json",
    )
    assert submit.status_code == 404  # can't even read it → 404, not a resolved gate
    assert ReviewRequest.objects.get(pk=review.id).status == ReviewRequest.STATUS_PENDING

    delete = _client(jj).delete(f"/api/reviews/{review.id}/")
    assert delete.status_code == 404
    assert ReviewRequest.objects.filter(pk=review.id).exists()  # not deleted

    # A member of connect CAN act on it — proving it's the boundary, not a total lock.
    member = _user("m@connect.example")
    ensure_member(connect, member, WorkspaceMembership.EDITOR)
    assert _client(member).get(f"/api/reviews/{review.id}/").status_code == 200
    assert _client(member).delete(f"/api/reviews/{review.id}/").status_code == 204


@override_settings(REQUIRE_AUTH=True)
def test_anonymous_can_still_read_public_link_review_after_scoping():
    """The detail read path must keep serving visibility=link reviews to anon —
    membership scoping applies to private reviews, but a public link stays public."""
    jj = _user("jj@dimagi.com", is_superuser=True)
    resp = _create_review(_client(jj), visibility="link")
    rid = resp.json()["id"]
    review = ReviewRequest.objects.get(pk=rid)
    assert review.workspace_id == DEFAULT_WORKSPACE_SLUG  # it IS workspace-scoped
    anon = Client().get(f"/api/reviews/{rid}/")
    assert anon.status_code == 200
    assert anon.json()["id"] == rid


@override_settings(REQUIRE_AUTH=True)
def test_submit_still_requires_auth_after_scoping():
    jj = _user("jj@dimagi.com", is_superuser=True)
    resp = _create_review(_client(jj), visibility="link")
    rid = resp.json()["id"]
    anon = Client().post(
        f"/api/reviews/{rid}/submit/",
        data=json.dumps({"response_json": {}}),
        content_type="application/json",
    )
    assert anon.status_code in (401, 403)
