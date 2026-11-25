"""Narrative-level visibility cascade + computed aggregate field."""
import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.reviews.models import ReviewRequest
from apps.runs import aggregate
from apps.walkthroughs.models import Walkthrough
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership


@pytest.fixture
def owner(db):
    u = get_user_model().objects.create_user(
        username="owner@dimagi.com", email="owner@dimagi.com",
    )
    # Content is workspace-scoped; give the owner a workspace + membership so the
    # DDD read-model (which filters by the caller's workspaces) sees it — mirrors
    # production, where the upload API always assigns a workspace.
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u)
    wsvc.ensure_member(ws, u, WorkspaceMembership.OWNER)
    return u


def _ws():
    return Workspace.objects.get(slug="dimagi")


def _wt(owner, **kw):
    defaults = dict(
        title="art", kind="video", owner=owner, workspace=_ws(),
        drive_file_id="f", drive_folder_id="d",
        content_type="video/mp4", size_bytes=1,
        run_id="demo-2026-06-09-001", narrative_slug="demo",
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


def _rev(owner, **kw):
    defaults = dict(
        run_id="demo-2026-06-09-001", narrative_slug="demo", workspace=_ws(),
        gate="narrative-agreement", request_json={"narrative": "s"}, owner=owner,
    )
    defaults.update(kw)
    return ReviewRequest.objects.create(**defaults)


def test_set_narrative_visibility_cascades(db, owner):
    w1 = _wt(owner, drive_file_id="f1", visibility="private")
    w2 = _wt(owner, drive_file_id="f2", visibility="private", run_id="demo-2026-06-09-002")
    r1 = _rev(owner, visibility="private")
    wt_n, rev_n = aggregate.set_narrative_visibility("demo", "link", workspace_slugs=aggregate.EVERY_WORKSPACE)
    assert (wt_n, rev_n) == (2, 1)
    w1.refresh_from_db(); w2.refresh_from_db(); r1.refresh_from_db()
    assert w1.visibility == w2.visibility == "link"
    assert r1.visibility == "link"


def test_aggregate_visibility_public_private_mixed(db, owner):
    _wt(owner, drive_file_id="fa", visibility="link")
    _rev(owner, visibility="link")
    assert aggregate.build_narrative("demo", workspace_slugs=aggregate.EVERY_WORKSPACE)["visibility"] == "public"
    aggregate.set_narrative_visibility("demo", "private", workspace_slugs=aggregate.EVERY_WORKSPACE)
    assert aggregate.build_narrative("demo", workspace_slugs=aggregate.EVERY_WORKSPACE)["visibility"] == "private"
    # Make one row disagree -> mixed.
    Walkthrough.objects.filter(narrative_slug="demo").update(visibility="link")
    assert aggregate.build_narrative("demo", workspace_slugs=aggregate.EVERY_WORKSPACE)["visibility"] == "mixed"


@override_settings(REQUIRE_AUTH=True)
def test_patch_endpoint_requires_auth(db, owner):
    _wt(owner)
    resp = Client().patch(
        "/api/ddd/narratives/demo/visibility/",
        data={"visibility": "link"}, content_type="application/json",
    )
    assert resp.status_code == 401


@override_settings(REQUIRE_AUTH=True)
def test_patch_endpoint_cascades(db, owner):
    _wt(owner, visibility="private")
    _rev(owner, visibility="private")
    client = Client(); client.force_login(owner)
    resp = client.patch(
        "/api/ddd/narratives/demo/visibility/",
        data={"visibility": "link"}, content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["visibility"] == "public"
    assert body["walkthroughs_updated"] == 1
    assert body["reviews_updated"] == 1
