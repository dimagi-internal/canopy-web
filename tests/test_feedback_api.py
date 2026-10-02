"""/api/feedback — ingest, list, resolve."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.feedback.models import Feedback

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    from apps.workspaces.testing import a_member, a_workspace

    return a_member(a_workspace("fb-api-ws"), email="jj@dimagi.com")


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
    assert again.json() == {"created": 0, "duplicate": 1, "empty": 0, "ids": []}
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
    assert fb.submitted_by.email == "jj@dimagi.com"


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


def test_a_note_with_no_words_is_not_stored(client):
    """The UI disables its submit button, but the API accepted an empty body and
    quietly created a row — an agent ingesting a mailbox could fill the pool
    with blanks that someone then has to triage."""
    r = client.post(
        "/api/feedback/",
        {"items": [{"target_ref": "verified-monitoring", "body": "   ", "channel": "email",
                    "source_ref": "<blank@mail>"}]},
        content_type="application/json",
    )
    assert r.status_code == 200
    assert r.json() == {"created": 0, "duplicate": 0, "empty": 1, "ids": []}
    assert Feedback.objects.count() == 0


def test_a_suggestion_counts_as_content_even_with_no_body(client):
    r = client.post(
        "/api/feedback/",
        {"items": [{"target_ref": "verified-monitoring", "kind": "suggestion",
                    "suggested_text": "Say back-check.", "channel": "email",
                    "source_ref": "<s@mail>"}]},
        content_type="application/json",
    )
    assert r.json()["created"] == 1


def test_another_tenant_cannot_read_or_resolve_feedback(client):
    """The pool had no tenant boundary: any signed-in user read every
    workspace's reviewer notes (author emails included) and resolved them."""
    from apps.workspaces.testing import a_member, a_workspace

    client.post("/api/feedback/", _batch(), content_type="application/json")
    pk = Feedback.objects.get().pk

    outsider = Client()
    outsider.force_login(a_member(a_workspace("fb-other-ws"), email="out@dimagi.com"))
    assert outsider.get("/api/feedback/").json()["items"] == []
    r = outsider.post(f"/api/feedback/{pk}/resolve", {"state": "declined"},
                      content_type="application/json")
    assert r.status_code == 404
    assert Feedback.objects.get(pk=pk).state == "new"


def test_a_user_in_no_workspace_cannot_file_feedback():
    from apps.workspaces.testing import a_workspace

    a_workspace()  # the org default exists, owned by someone else
    nobody = Client()
    nobody.force_login(User.objects.create_user("nobody", "nobody@dimagi.com", "pw"))
    r = nobody.post("/api/feedback/", _batch(), content_type="application/json")
    assert r.status_code == 422
