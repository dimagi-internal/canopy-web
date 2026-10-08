"""Clean, shareable DDD run release page (/api/ddd/release/<run_id>/).

Covers the two halves of the share path: the visibility cascade must now MINT a
share token on flip-to-public, and the release endpoint must be reachable
anonymously with a matching ?t= token (and 404 without one) while giving members
the extra internal affordances.
"""
import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.reviews.models import ReviewRequest
from apps.runs import aggregate
from apps.walkthroughs.models import Walkthrough
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

RUN_ID = "demo-2026-07-22-001"


@pytest.fixture
def owner(db):
    u = get_user_model().objects.create_user(
        username="owner@dimagi.com", email="owner@dimagi.com",
    )
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u)
    wsvc.ensure_member(ws, u, WorkspaceMembership.OWNER)
    return u


def _ws():
    return Workspace.objects.get(slug="dimagi")


def _hero(owner, **kw):
    defaults = dict(
        title="Hero", kind="video", role=Walkthrough.ROLE_HERO_VIDEO,
        owner=owner, workspace=_ws(), drive_file_id="hero", drive_folder_id="d",
        content_type="video/mp4", size_bytes=1, duration_sec=100,
        run_id=RUN_ID, narrative_slug="demo",
        links=[{"label": "Program Admin Report", "url": "https://labs/par", "kind": "reference"}],
    )
    defaults.update(kw)
    return Walkthrough.objects.create(**defaults)


def _rev(owner, **kw):
    defaults = dict(
        run_id=RUN_ID, narrative_slug="demo", workspace=_ws(),
        gate="narrative-agreement",
        request_json={"narrative": "The Story Title\nA program manager reads three managers."},
        owner=owner,
    )
    defaults.update(kw)
    return ReviewRequest.objects.create(**defaults)


# --- cascade now mints tokens ------------------------------------------------

def test_cascade_mints_share_tokens_on_public(db, owner):
    w = _hero(owner, visibility="private")
    assert not w.share_token
    aggregate.set_narrative_visibility("demo", "link")
    w.refresh_from_db()
    assert w.visibility == "link"
    assert w.share_token, "flip-to-public must mint a share token"


def test_cascade_to_private_leaves_token(db, owner):
    w = _hero(owner, visibility="private")
    aggregate.set_narrative_visibility("demo", "link")
    w.refresh_from_db()
    tok = w.share_token
    aggregate.set_narrative_visibility("demo", "private")
    w.refresh_from_db()
    assert w.visibility == "private"
    assert w.share_token == tok, "flip-to-private keeps the token (revive later)"


# --- the release endpoint ----------------------------------------------------

@override_settings(REQUIRE_AUTH=True)
def test_release_anonymous_without_token_404s(db, owner):
    _hero(owner, visibility="private")
    _rev(owner)
    resp = Client().get(f"/api/ddd/release/{RUN_ID}/")
    assert resp.status_code == 404  # never leaks existence


@override_settings(REQUIRE_AUTH=True)
def test_release_anonymous_with_valid_token(db, owner):
    _hero(owner, visibility="private")
    _rev(owner)
    aggregate.set_narrative_visibility("demo", "link")
    token = Walkthrough.objects.get(run_id=RUN_ID).share_token

    resp = Client().get(f"/api/ddd/release/{RUN_ID}/?t={token}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == RUN_ID
    assert body["is_public"] is True
    assert body["is_member"] is False
    assert body["share_token"] == token
    # Stream URL carries the artifact's own token so anonymous playback works.
    w = Walkthrough.objects.get(run_id=RUN_ID)
    assert body["video"]["content_url"] == f"/w/{w.workspace_id}/walkthrough/{w.id}/content?t={token}"
    # Product URLs surfaced as named links; no operator jargon fields.
    assert body["product_links"] == [
        {"label": "Program Admin Report", "url": "https://labs/par", "kind": "reference"}
    ]
    assert "phase" not in body and "all_artifacts" not in body


@override_settings(REQUIRE_AUTH=True)
def test_release_anonymous_wrong_token_404s(db, owner):
    _hero(owner, visibility="private")
    aggregate.set_narrative_visibility("demo", "link")
    resp = Client().get(f"/api/ddd/release/{RUN_ID}/?t=not-the-token")
    assert resp.status_code == 404


# --- link curation + title -----------------------------------------------

def test_clean_links_drops_junk_and_dedupes():
    # The ${var} link is FIRST so it would pollute host derivation if the helper
    # naively took the first absolute-looking url (the real nutrition-demo bug).
    raw = [
        {"label": "template", "url": "https://labs.x${par_url}", "kind": "reference"},  # unresolved var, first
        {"label": "PAR", "url": "https://labs.x/labs/workflow/5003/run/?program_id=1", "kind": "reference"},
        {"label": "PAR again", "url": "/labs/workflow/5003/run/?program_id=1", "kind": "reference"},  # host-less dup
        {"label": "App", "url": "https://labs.x", "kind": "reference"},  # bare origin
        {"label": "App/", "url": "https://labs.x/", "kind": "reference"},  # bare origin w/ slash
        {"label": "Audit", "url": "https://labs.x/audit/4996/bulk/", "kind": "reference"},
    ]
    out = aggregate._clean_links(raw)
    urls = [l["url"] for l in out]
    assert urls == [
        "https://labs.x/labs/workflow/5003/run/?program_id=1",  # relative dup absolutized to clean host + collapsed
        "https://labs.x/audit/4996/bulk/",
    ]
    assert not any("${" in u for u in urls)  # no leak survives, even via absolutization


@override_settings(REQUIRE_AUTH=True)
def test_release_title_falls_back_when_story_line_is_long(db, owner):
    _hero(owner, visibility="private")
    long_first_line = "Priya runs a nutrition program funded to deliver RUTF across three managers"
    _rev(owner, request_json={"narrative": f"{long_first_line}\nShe reviews compliance weekly."})
    client = Client(); client.force_login(owner)
    body = client.get(f"/api/ddd/release/{RUN_ID}/").json()
    assert body["title"] == "Demo"  # humanized slug, not the 74-char sentence
    assert body["lede"] and body["lede"].startswith("Priya runs")  # sentence becomes the lede


@override_settings(REQUIRE_AUTH=True)
def test_release_member_gets_internal_affordances(db, owner):
    _hero(owner, visibility="private")  # not public
    _rev(owner)
    client = Client(); client.force_login(owner)
    resp = client.get(f"/api/ddd/release/{RUN_ID}/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_member"] is True
    assert body["is_public"] is False
    assert body["build_url"] == f"/w/dimagi/ddd/demo/{RUN_ID}"
    # Private artifact → tokenless stream URL (session auth covers it).
    assert body["video"]["content_url"].endswith("/content")


@override_settings(REQUIRE_AUTH=True, CANOPY_PUBLIC_BASE_URL="https://canopy.example.org")
def test_release_page_lives_under_its_workspace(db, owner):
    """The release page is `/w/<ws>/ddd-release/…` (canopy-web#1337): the API
    hands out that address, and answers only the page that names the run's own
    workspace — the flat `/ddd-release/…` is a 404."""
    _hero(owner, visibility="private")
    _rev(owner)
    aggregate.set_narrative_visibility("demo", "link")
    token = Walkthrough.objects.get(run_id=RUN_ID).share_token

    body = Client().get(f"/api/ddd/release/{RUN_ID}/?t={token}&ws=dimagi").json()
    assert body["share_url"] == f"https://canopy.example.org/w/dimagi/ddd-release/demo/{RUN_ID}?t={token}"
    assert Client().get(f"/api/ddd/release/{RUN_ID}/?t={token}&ws=connect").status_code == 404

    page = Client().get(f"/ddd-release/demo/{RUN_ID}?t={token}")
    assert page.status_code == 404 and "Location" not in page
