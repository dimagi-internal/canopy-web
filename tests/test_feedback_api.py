"""/api/feedback — ingest, list, resolve."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def client(user):
    c = Client()
    c.force_login(user)
    return c


def _batch(**over):
    item = dict(
        target_kind="narrative",
        target_ref="verified-monitoring",
        target_version=17,
        anchor_id="the-goal",
        kind="comment",
        body="'Back-check' is the term of art; 'audit' means something else.",
        author_name="Sophie",
        channel="email",
        source_ref="<m1@mail>",
    )
    item.update(over)
    return {"items": [item]}


def test_post_requires_auth():
    anon = Client()
    r = anon.post("/api/feedback/", _batch(), content_type="application/json")
    assert r.status_code in (401, 403)


def test_post_creates_and_is_idempotent(client):
    first = client.post("/api/feedback/", _batch(), content_type="application/json")
    assert first.status_code == 200, first.content
    assert first.json()["created"] == 1

    again = client.post("/api/feedback/", _batch(), content_type="application/json")
    assert again.json() == {"created": 0, "duplicate": 1, "ids": []}
    assert Feedback.objects.count() == 1


def test_list_filters(client):
    client.post("/api/feedback/", _batch(), content_type="application/json")
    client.post(
        "/api/feedback/",
        _batch(target_ref="other", source_ref="<m2@mail>"),
        content_type="application/json",
    )
    r = client.get("/api/feedback/?target_ref=verified-monitoring")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


def test_resolve_records_disposition(client):
    fid = client.post(
        "/api/feedback/", _batch(), content_type="application/json"
    ).json()["ids"][0]
    r = client.post(
        f"/api/feedback/{fid}/resolve",
        {"state": "answered", "note": "folded into v18", "resolved_in_version": 18},
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "answered"
    assert body["resolved_in_version"] == 18


def test_resolve_404s_on_a_missing_row(client):
    r = client.post(
        "/api/feedback/999999/resolve",
        {"state": "answered"},
        content_type="application/json",
    )
    assert r.status_code == 404


def test_submitted_by_is_the_caller_not_the_author(client):
    client.post("/api/feedback/", _batch(), content_type="application/json")
    fb = Feedback.objects.get()
    assert fb.author_name == "Sophie"
    assert fb.submitted_by is not None
    assert fb.submitted_by.username == "jj"


def test_an_unknown_field_is_rejected_rather_than_silently_dropped(client):
    """StrictModel: a typo'd key must 422, not vanish."""
    payload = _batch()
    payload["items"][0]["athor_name"] = "typo"
    r = client.post("/api/feedback/", payload, content_type="application/json")
    assert r.status_code == 422


def test_a_suggestion_round_trips_its_proposed_text(client):
    client.post(
        "/api/feedback/",
        _batch(kind="suggestion", suggested_text="…a re-visit by a QC enumerator."),
        content_type="application/json",
    )
    r = client.get("/api/feedback/?target_ref=verified-monitoring")
    item = r.json()["items"][0]
    assert item["kind"] == "suggestion"
    assert "QC enumerator" in item["suggested_text"]
