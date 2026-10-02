"""Contract tests for the reviews Ninja surface (/api/reviews/).

Covers:
  POST   /api/reviews/              — create; returns id + url
  GET    /api/reviews/<id>/         — authenticated users see all; link-visibility readable by anyone
  POST   /api/reviews/<id>/submit/  — writes response_json, flips status, stamps resolved_at (auth required)
  Round-trip: create → get reflects request_json correctly
  Submit gate: already-resolved → 403
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.reviews.models import ReviewRequest

from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace

User = get_user_model()

BASE = "/api/reviews"

# ---------------------------------------------------------------------------
# Minimal valid request_json matching the canopy ReviewRequest schema
# ---------------------------------------------------------------------------

SAMPLE_REQUEST_JSON = {
    "schema_version": 1,
    "run_id": "run-abc-123",
    "gate": "pre_ship",
    "video": {"walkthrough_id": "walk-001", "url": "https://example.com/w/001"},
    "decisions": [
        {
            "id": "d1",
            "prompt": "Should we ship?",
            "options": ["yes", "no", "defer"],
            "recommended": "yes",
            "class": "go_no_go",
        }
    ],
    "narration": [{"scene": 1, "id": "n1", "text": "The demo starts here."}],
    "autonomous_audit": ["No regressions detected."],
}

SAMPLE_RESPONSE_JSON = {
    "decisions": {"d1": "yes"},
    "narration_edits": {"n1": "Updated narration text."},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _editor(email):
    # Editors of the default workspace: submitting and deleting a review is the
    # author tier, and a review with no workspace is visible to no member.
    user = User.objects.create_user(username=email, email=email)
    ensure_member(a_workspace(), user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def owner(db):
    return _editor("owner@dimagi.com")


@pytest.fixture
def other_user(db):
    return _editor("other@dimagi.com")


@pytest.fixture
def auth_client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture
def other_client(other_user):
    c = Client()
    c.force_login(other_user)
    return c


def _make_review(owner, **kwargs) -> ReviewRequest:
    defaults = dict(
        run_id="run-test",
        gate="pre_ship",
        request_json=SAMPLE_REQUEST_JSON,
        visibility="link",
        workspace=a_workspace(),
    )
    defaults.update(kwargs)
    return ReviewRequest.objects.create(owner=owner, **defaults)


# ---------------------------------------------------------------------------
# 1. create returns id + url
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_returns_id_and_url(auth_client):
    resp = auth_client.post(
        f"{BASE}/",
        data={"request_json": SAMPLE_REQUEST_JSON, "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert "id" in body
    assert "url" in body
    # A link-visibility review returns a share token (the external-suggest capability).
    assert body.get("share_token")
    # URL should reference the review id
    assert body["id"] in body["url"]


@pytest.mark.django_db
def test_create_rejects_overlong_fields_with_422_not_500(auth_client):
    # run_id/gate/narrative_slug come from the unbounded request_json but map to
    # fixed-width columns; an over-length value must 422 at the boundary, not reach
    # Postgres and 500 (SQLite CI wouldn't catch the 500).
    over = {**SAMPLE_REQUEST_JSON, "run_id": "r" * 300}  # column is max_length=255
    resp = auth_client.post(
        f"{BASE}/",
        data={"request_json": over, "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 422, resp.content
    assert "run_id" in resp.content.decode()


# ---------------------------------------------------------------------------
# 2. GET by owner returns full record
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_by_owner_returns_full_record(auth_client, owner):
    review = _make_review(owner, visibility="link")

    resp = auth_client.get(f"{BASE}/{review.id}/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["id"] == str(review.id)
    assert body["is_owner"] is True
    assert body["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. GET unauthenticated on a link-visibility review returns data
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_link_visibility_unauth_returns_data(owner):
    """Unauthenticated callers can read link-visibility reviews without a token."""
    review = _make_review(owner, visibility="link")

    c = Client()  # unauthenticated
    resp = c.get(f"{BASE}/{review.id}/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "pending"
    assert body["is_owner"] is False
    assert body["run_id"] == review.run_id
    assert body["gate"] == review.gate


# ---------------------------------------------------------------------------
# 4. GET unauthenticated on a private review → 404
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_private_review_unauth_returns_404(owner):
    review = _make_review(owner, visibility="private")

    c = Client()
    resp = c.get(f"{BASE}/{review.id}/")
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_get_nonexistent_review_404(auth_client):
    bogus = uuid.uuid4()
    resp = auth_client.get(f"{BASE}/{bogus}/")
    assert resp.status_code == 404, resp.content
    body = resp.json()
    assert body.get("type", "").endswith("/not-found")


# ---------------------------------------------------------------------------
# 5. submit writes response_json + flips status + stamps resolved_at
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_submit_resolves_review(auth_client, owner):
    review = _make_review(owner, visibility="link")

    resp = auth_client.post(
        f"{BASE}/{review.id}/submit/",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "resolved"
    assert body["response_json"] == SAMPLE_RESPONSE_JSON
    assert body["resolved_at"] is not None

    # Verify DB state
    review.refresh_from_db()
    assert review.status == ReviewRequest.STATUS_RESOLVED
    assert review.response_json == SAMPLE_RESPONSE_JSON
    assert review.resolved_at is not None


# ---------------------------------------------------------------------------
# 6. submit requires authentication — unauthenticated → 403
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_submit_unauth_returns_403(owner):
    """Unauthenticated callers can read link-visibility reviews but cannot submit."""
    review = _make_review(owner, visibility="link")

    c = Client()
    resp = c.post(
        f"{BASE}/{review.id}/submit/",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 403, resp.content


# ---------------------------------------------------------------------------
# 8. re-submission on already-resolved → 403
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_submit_already_resolved_returns_403(auth_client, owner):
    review = _make_review(owner, visibility="link")
    review.status = ReviewRequest.STATUS_RESOLVED
    review.response_json = SAMPLE_RESPONSE_JSON
    review.resolved_at = timezone.now()
    review.save()

    resp = auth_client.post(
        f"{BASE}/{review.id}/submit/",
        data={"response_json": {"decisions": {}, "narration_edits": {}}},
        content_type="application/json",
    )
    assert resp.status_code == 403, resp.content
    body = resp.json()
    assert body.get("type", "").endswith("/forbidden")


# ---------------------------------------------------------------------------
# 9. request_json round-trips: create → get reflects the same JSON
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_request_json_round_trips(auth_client, owner):
    resp = auth_client.post(
        f"{BASE}/",
        data={"request_json": SAMPLE_REQUEST_JSON, "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    rid = resp.json()["id"]

    resp2 = auth_client.get(f"{BASE}/{rid}/")
    assert resp2.status_code == 200, resp2.content
    body = resp2.json()
    assert body["request_json"] == SAMPLE_REQUEST_JSON
    assert body["run_id"] == SAMPLE_REQUEST_JSON["run_id"]
    assert body["gate"] == SAMPLE_REQUEST_JSON["gate"]


# ---------------------------------------------------------------------------
# 10. non-owner authenticated user can still read (team-internal access)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_non_owner_authenticated_can_read(other_client, owner):
    review = _make_review(owner, visibility="private")

    resp = other_client.get(f"{BASE}/{review.id}/")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["is_owner"] is False


# ---------------------------------------------------------------------------
# 11. submit by an authenticated NON-owner is allowed (team-internal write)
#
#     Write gate semantics: any authenticated user may submit, just like
#     any authenticated user may read — reviews are team-internal resources.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_submit_by_authenticated_non_owner_is_allowed(other_client, owner):
    """Authenticated non-owner can submit a review (team-internal write access)."""
    review = _make_review(owner, visibility="private")

    resp = other_client.post(
        f"{BASE}/{review.id}/submit/",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["status"] == "resolved"

    review.refresh_from_db()
    assert review.status == ReviewRequest.STATUS_RESOLVED


# ---------------------------------------------------------------------------
# 12. dashboard LIST — auth required, derived fields, search, status, order
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_list_requires_auth():
    c = Client()  # unauthenticated
    resp = c.get(f"{BASE}/")
    assert resp.status_code == 401, resp.content


@pytest.mark.django_db
def test_list_returns_all_with_derived_fields(auth_client, owner):
    _make_review(
        owner,
        run_id="microplans-study-design-2026-05-29-001",
        gate="concept_change",
        request_json={
            "schema_version": 1,
            "run_id": "microplans-study-design-2026-05-29-001",
            "gate": "concept_change",
            "narrative": "Maya turns delivery into a defensible study.\nSecond line.",
            "narration": [
                {"scene": 1, "id": "s1", "title": "Open designer", "text": "x"},
                {"scene": 2, "id": "s2", "title": "See delivery", "text": "y"},
            ],
            "decisions": [],
            "autonomous_audit": [],
        },
    )

    resp = auth_client.get(f"{BASE}/")
    assert resp.status_code == 200, resp.content
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    # run_id stamp stripped → clean narrative_slug label
    assert row["narrative_slug"] == "microplans-study-design"
    # title is the narrative's first line
    assert row["title"] == "Maya turns delivery into a defensible study."
    assert row["scene_count"] == 2
    assert row["gate"] == "concept_change"
    assert row["status"] == "pending"
    # last_activity_at falls back to created_at while pending
    assert row["last_activity_at"] == row["created_at"]


@pytest.mark.django_db
def test_list_search_filters_by_feature(auth_client, owner):
    _make_review(owner, run_id="alpha-narrative_slug-2026-05-01-001")
    _make_review(owner, run_id="beta-thing-2026-05-01-001")

    resp = auth_client.get(f"{BASE}/?q=beta")
    assert resp.status_code == 200, resp.content
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["narrative_slug"] == "beta-thing"


@pytest.mark.django_db
def test_list_status_filter(auth_client, owner):
    _make_review(owner, run_id="p-2026-05-01-001")  # pending
    resolved = _make_review(owner, run_id="r-2026-05-01-001")
    resolved.status = ReviewRequest.STATUS_RESOLVED
    resolved.resolved_at = timezone.now()
    resolved.save()

    resp = auth_client.get(f"{BASE}/?status=resolved")
    assert resp.status_code == 200, resp.content
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"
    # resolved row reports its resolved_at as last activity
    assert rows[0]["last_activity_at"] == rows[0]["resolved_at"]


@pytest.mark.django_db
def test_list_orders_by_last_activity_desc_by_default(auth_client, owner):
    older = _make_review(owner, run_id="older-2026-05-01-001")
    _make_review(owner, run_id="newer-2026-05-01-001")  # the row matters, not the binding
    # Resolve the older one *now* so its last_activity jumps ahead of newer's.
    older.status = ReviewRequest.STATUS_RESOLVED
    older.resolved_at = timezone.now()
    older.save()

    resp = auth_client.get(f"{BASE}/")
    assert resp.status_code == 200, resp.content
    features = [r["narrative_slug"] for r in resp.json()]
    assert features[0] == "older"  # most-recently-edited first


# ---------------------------------------------------------------------------
# 13. dashboard DELETE — auth required, removes record, 404 on missing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_requires_auth(owner):
    review = _make_review(owner)
    c = Client()  # unauthenticated
    resp = c.delete(f"{BASE}/{review.id}/")
    assert resp.status_code == 401, resp.content
    assert ReviewRequest.objects.filter(pk=review.id).exists()


@pytest.mark.django_db
def test_delete_removes_record(auth_client, owner):
    review = _make_review(owner)
    resp = auth_client.delete(f"{BASE}/{review.id}/")
    assert resp.status_code == 204, resp.content
    assert not ReviewRequest.objects.filter(pk=review.id).exists()


@pytest.mark.django_db
def test_delete_by_non_owner_authenticated_allowed(other_client, owner):
    """Team-internal cleanup: any authenticated user may delete (reviews are often
    owned by the orchestrator PAT, not the human tidying up)."""
    review = _make_review(owner)
    resp = other_client.delete(f"{BASE}/{review.id}/")
    assert resp.status_code == 204, resp.content
    assert not ReviewRequest.objects.filter(pk=review.id).exists()


@pytest.mark.django_db
def test_delete_nonexistent_returns_404(auth_client):
    resp = auth_client.delete(f"{BASE}/{uuid.uuid4()}/")
    assert resp.status_code == 404, resp.content


# ---------------------------------------------------------------------------
# product_findings is a RUN-CHILD, not a narrative version
# ---------------------------------------------------------------------------

PRODUCT_FINDINGS_REQUEST_JSON = {
    "schema_version": 1,
    "run_id": "program-admin-report-2026-06-11-001",
    "gate": "product_findings",
    "feature": "program-admin-report",
    "iteration": 3,
    "video": {"url": "https://example.com/w/clip/content"},
    "deck_url": "https://example.com/w/deck",
    "summary": {"concept_score": 2, "user_score": 2, "verdict": "FAIL"},
    "clusters": [
        {
            "id": "closed-record-readonly",
            "title": "Completed audits show enabled mutation buttons",
            "severity": "high",
            "fix_kind": "mechanical",
            "route": "PRODUCT",
            "scenes": [9, 10],
            "suggested_fix": "Lock state on completed audits.",
            "count": 2,
            "evidence": [
                {"scene": 9, "thumb": "data:image/jpeg;base64,AAAA", "deck_anchor": "#scene-9", "video_t": 84}
            ],
        }
    ],
}


@pytest.mark.django_db
def test_product_findings_is_run_child_not_a_narrative_version(auth_client):
    """A product_findings review must NOT pollute the narrative version timeline.

    It posts with narrative_slug=None and version=0 (the run-child sentinel), even
    though the run_id slug would otherwise derive a narrative_slug — so it never
    shows up as a "v3" row in the DDD shell.
    """
    resp = auth_client.post(
        f"{BASE}/",
        data={"request_json": PRODUCT_FINDINGS_REQUEST_JSON, "visibility": "link"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    review = ReviewRequest.objects.get(id=resp.json()["id"])
    assert review.gate == "product_findings"
    assert review.narrative_slug is None
    assert review.version == 0


@pytest.mark.django_db
def test_product_findings_does_not_bump_narrative_version(auth_client):
    """Posting a product_findings review between narrative versions leaves the
    narrative version counter untouched (1, 2 — not 1, skip, 3)."""
    narrative_post = {**SAMPLE_REQUEST_JSON, "gate": "concept_change", "run_id": "feat-x-2026-01-01-001"}
    first = auth_client.post(
        f"{BASE}/", data={"request_json": narrative_post, "visibility": "link"}, content_type="application/json"
    )
    v1 = ReviewRequest.objects.get(id=first.json()["id"]).version

    findings_post = {**PRODUCT_FINDINGS_REQUEST_JSON, "run_id": "feat-x-2026-01-01-001"}
    auth_client.post(
        f"{BASE}/", data={"request_json": findings_post, "visibility": "link"}, content_type="application/json"
    )

    second = auth_client.post(
        f"{BASE}/", data={"request_json": narrative_post, "visibility": "link"}, content_type="application/json"
    )
    v2 = ReviewRequest.objects.get(id=second.json()["id"]).version
    assert (v1, v2) == (1, 2)


# ---------------------------------------------------------------------------
# Run-child gates carry no narrative_slug
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_child_review_reports_no_narrative_slug(auth_client, owner):
    """create_review deliberately stores narrative_slug=NULL for a run-child gate,
    but both serializers re-derived one from the run_id — handing the frontend a
    slug the model had explicitly refused. That derived slug is what lit up a
    phantom narrative in the DDD rail."""
    review = _make_review(
        owner,
        run_id="ada-fleet-audit-2026-07-14",
        gate="product_findings",
        narrative_slug=None,
        request_json={"run_id": "ada-fleet-audit-2026-07-14", "gate": "product_findings"},
    )

    detail = auth_client.get(f"{BASE}/{review.id}/").json()
    assert detail["narrative_slug"] is None

    listed = auth_client.get(f"{BASE}/").json()
    row = next(r for r in listed if r["id"] == str(review.id))
    assert row["narrative_slug"] is None


@pytest.mark.django_db
def test_narrative_gate_still_reports_its_slug(auth_client, owner):
    """The counterpart: a narrative-gate review keeps its slug. Legacy rows stored
    before the column existed have it derived from the run_id, as before."""
    stored = _make_review(
        owner,
        run_id="microplans-2026-06-02-001",
        gate="concept_change",
        narrative_slug="microplans",
    )
    legacy = _make_review(
        owner,
        run_id="microplans-2026-06-02-002",
        gate="concept_change",
        narrative_slug=None,
    )

    assert auth_client.get(f"{BASE}/{stored.id}/").json()["narrative_slug"] == "microplans"
    assert auth_client.get(f"{BASE}/{legacy.id}/").json()["narrative_slug"] == "microplans"


@pytest.mark.django_db
def test_list_filter_and_sort_survive_a_null_narrative_slug(auth_client, owner):
    """?q= and ?order=narrative_slug both called .lower() on the slug bare, so a
    NULL one 500s the whole list rather than just excluding that row."""
    _make_review(
        owner,
        run_id="ada-fleet-audit-2026-07-14",
        gate="product_findings",
        narrative_slug=None,
    )
    _make_review(
        owner,
        run_id="microplans-2026-06-02-001",
        gate="concept_change",
        narrative_slug="microplans",
    )

    assert auth_client.get(f"{BASE}/?order=narrative_slug").status_code == 200

    hits = auth_client.get(f"{BASE}/?q=microplans").json()
    assert [r["narrative_slug"] for r in hits] == ["microplans"]

    # A run-child review has no slug to match on, but is still findable by run_id.
    hits = auth_client.get(f"{BASE}/?q=fleet-audit").json()
    assert [r["run_id"] for r in hits] == ["ada-fleet-audit-2026-07-14"]


# ---------------------------------------------------------------------------
# External-reviewer suggestions (share-token; NEVER resolves the gate)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_private_has_no_share_token(auth_client):
    resp = auth_client.post(
        f"{BASE}/",
        data={"request_json": SAMPLE_REQUEST_JSON, "visibility": "private"},
        content_type="application/json",
    )
    assert resp.status_code == 201, resp.content
    assert resp.json().get("share_token") is None


@pytest.mark.django_db
def test_suggest_without_token_403(owner):
    review = _make_review(owner, visibility="link")
    review.ensure_share_token()
    resp = Client().post(  # anonymous, no ?t=
        f"{BASE}/{review.id}/suggest/",
        data={"response_json": SAMPLE_RESPONSE_JSON, "name": "Sophie"},
        content_type="application/json",
    )
    assert resp.status_code == 403, resp.content
    review.refresh_from_db()
    assert review.status == "pending"


@pytest.mark.django_db
def test_suggest_wrong_token_403(owner):
    review = _make_review(owner, visibility="link")
    review.ensure_share_token()
    resp = Client().post(
        f"{BASE}/{review.id}/suggest/?t=not-the-real-token",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 403, resp.content


@pytest.mark.django_db
def test_suggest_on_private_review_403(owner):
    # A private review is unreadable to an anonymous caller -> 404 (no existence leak),
    # and it never carries a usable external-suggest token anyway.
    review = _make_review(owner, visibility="private")
    review.ensure_share_token()
    resp = Client().post(
        f"{BASE}/{review.id}/suggest/?t={review.share_token}",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 404, resp.content


@pytest.mark.django_db
def test_suggest_with_token_appends_and_stays_pending(owner):
    review = _make_review(owner, visibility="link")
    token = review.ensure_share_token()
    resp = Client().post(  # external, unauthenticated
        f"{BASE}/{review.id}/suggest/?t={token}",
        data={
            "response_json": {"edited_scenes": [{"id": "n1", "narration": "softer wording"}]},
            "name": "Sophie",
        },
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["ok"] is True
    assert body["suggestion_count"] == 1

    review.refresh_from_db()
    # A suggestion must NEVER resolve the gate or overwrite the owner's response.
    assert review.status == "pending"
    assert review.response_json is None
    assert len(review.suggestions_json) == 1
    assert review.suggestions_json[0]["name"] == "Sophie"
    assert review.suggestions_json[0]["response_json"]["edited_scenes"][0]["id"] == "n1"


@pytest.mark.django_db
def test_suggestions_accumulate(owner):
    review = _make_review(owner, visibility="link")
    token = review.ensure_share_token()
    for i in range(3):
        Client().post(
            f"{BASE}/{review.id}/suggest/?t={token}",
            data={"response_json": {"overall_feedback": f"note {i}"}},
            content_type="application/json",
        )
    review.refresh_from_db()
    assert len(review.suggestions_json) == 3


@pytest.mark.django_db
def test_suggestions_visible_to_owner_not_anon(auth_client, owner):
    review = _make_review(owner, visibility="link")
    token = review.ensure_share_token()
    Client().post(
        f"{BASE}/{review.id}/suggest/?t={token}",
        data={"response_json": SAMPLE_RESPONSE_JSON, "name": "Sophie"},
        content_type="application/json",
    )
    # Owner sees the suggestion.
    owner_body = auth_client.get(f"{BASE}/{review.id}/").json()
    assert len(owner_body["suggestions"]) == 1
    # Anonymous link reader does NOT — else one external reviewer reads another's edits.
    anon_body = Client().get(f"{BASE}/{review.id}/").json()
    assert anon_body["suggestions"] == []


@pytest.mark.django_db
def test_suggest_on_resolved_review_403(auth_client, owner):
    review = _make_review(owner, visibility="link")
    token = review.ensure_share_token()
    auth_client.post(
        f"{BASE}/{review.id}/submit/",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    resp = Client().post(
        f"{BASE}/{review.id}/suggest/?t={token}",
        data={"response_json": SAMPLE_RESPONSE_JSON},
        content_type="application/json",
    )
    assert resp.status_code == 403, resp.content
