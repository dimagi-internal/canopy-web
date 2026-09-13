"""Contract tests for /api/issues/ — canopy.origin records (upsert/get/delete, auth, pointers-only)."""
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.issues.models import OriginIssue
from apps.issues.schemas import OriginIssueOut

User = get_user_model()

REC = {
    "repo": "jjackson/canopy", "number": 42, "title": "Codify the DDD operating model",
    "initiative": "ddd", "ledger": "dimagi-internal/hal/ledgers/ddd.md",
    "created": "2026-06-23", "confidence": "high",
    "mandate": "Encode the DDD operating model into the ddd skills.",
    "done_when": "A cold /canopy:ddd run follows it unprompted.",
    "intent": "DDD = a general methodology where one narrative does triple duty.",
    "evidence": [{"claim": "never review PRs", "session": "/Users/x/.claude/projects/p/s1.jsonl"}],
    "corpus": {"sessions_scanned": 331, "cross_user": True,
               "drilled": ["/Users/x/.claude/projects/p/s1.jsonl"]},
}


def _client():
    u = User.objects.create_user(username="alice", email="alice@dimagi.com", password="pw")
    c = Client()
    c.force_login(u)
    return c


@pytest.mark.django_db
def test_upsert_get_delete_roundtrip():
    c = _client()
    r = c.post("/api/issues/", data=json.dumps(REC), content_type="application/json")
    assert r.status_code == 201
    OriginIssueOut.model_validate(r.json())

    # idempotent re-sync → 200, no duplicate row
    r2 = c.post("/api/issues/", data=json.dumps({**REC, "confidence": "medium"}),
                content_type="application/json")
    assert r2.status_code == 200
    assert OriginIssue.objects.count() == 1
    assert r2.json()["confidence"] == "medium"

    g = c.get("/api/issues/jjackson__canopy/42/")
    assert g.status_code == 200
    assert g.json()["intent"].startswith("DDD =")

    # DELETE — the cleanup path JJ asked for
    d = c.delete("/api/issues/jjackson__canopy/42/")
    assert d.status_code == 204
    assert OriginIssue.objects.count() == 0
    assert c.get("/api/issues/jjackson__canopy/42/").status_code == 404


@pytest.mark.django_db
def test_requires_auth():
    assert Client().get("/api/issues/").status_code == 401
    assert Client().post("/api/issues/", data=json.dumps(REC),
                         content_type="application/json").status_code == 401


@pytest.mark.django_db
def test_stores_pointers_not_transcripts():
    c = _client()
    c.post("/api/issues/", data=json.dumps(REC), content_type="application/json")
    obj = OriginIssue.objects.get(repo="jjackson/canopy", number=42)
    assert obj.corpus["drilled"] == ["/Users/x/.claude/projects/p/s1.jsonl"]   # a path, not contents
    assert obj.evidence[0]["session"].endswith(".jsonl")


@pytest.mark.django_db
def test_list_filters_by_initiative():
    c = _client()
    c.post("/api/issues/", data=json.dumps(REC), content_type="application/json")
    c.post("/api/issues/", data=json.dumps({**REC, "number": 43, "initiative": "walkthrough"}),
           content_type="application/json")
    body = c.get("/api/issues/?initiative=ddd").json()
    assert body["total"] == 1 and body["items"][0]["initiative"] == "ddd"


@pytest.mark.django_db
def test_a_non_member_cannot_see_get_delete_or_overwrite_another_workspaces_issue():
    from apps.workspaces.models import Workspace

    # An origin record owned by "connect" (created by a connect member).
    connect_owner = User.objects.create_user(username="c", email="c@connect.example", password="pw")
    connect = Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=connect_owner, self_join_domains=[]
    )
    OriginIssue.objects.create(repo="jjackson/canopy", number=42, title="secret", workspace=connect)

    # alice is dimagi-only — not a member of connect.
    c = _client()
    assert all(o["repo"] != "jjackson/canopy" for o in c.get("/api/issues/").json()["items"])  # not listed
    assert c.get("/api/issues/jjackson__canopy/42/").status_code == 404
    assert c.delete("/api/issues/jjackson__canopy/42/").status_code == 404
    assert OriginIssue.objects.filter(number=42).exists()  # not deleted

    # Upsert of the same (repo, number) must NOT overwrite connect's record.
    r = c.post("/api/issues/", data=json.dumps({**REC, "title": "hijacked"}),
               content_type="application/json")
    assert r.status_code == 404
    assert OriginIssue.objects.get(number=42).title == "secret"  # untouched
