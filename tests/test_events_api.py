"""/api/events — record (coalescing) and read (tenant-scoped)."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def user():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw")


@pytest.fixture()
def workspace(user):
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(workspace=ws, user=user, role="owner")
    return ws


@pytest.fixture()
def client(user, workspace):
    c = Client()
    c.force_login(user)
    return c


def _batch(**over):
    item = dict(
        source="inbound.gmail",
        kind="gmail.push.missed",
        level="error",
        key="eva@dimagi-ai.com",
        summary="poll found a message push never rang for",
        payload={"mailbox": "eva@dimagi-ai.com"},
    )
    item.update(over)
    return {"items": [item]}


def test_post_requires_auth():
    anon = Client()
    r = anon.post("/api/events/", _batch(), content_type="application/json")
    assert r.status_code in (401, 403)


def test_record_creates_a_row(client, workspace):
    r = client.post("/api/events/", _batch(), content_type="application/json")
    assert r.status_code == 200, r.content
    assert r.json() == {"created": 1, "coalesced": 0}
    ev = Event.objects.get()
    assert ev.workspace_id == workspace.pk
    assert ev.level == "error"
    assert ev.count == 1


def test_a_repeat_coalesces_instead_of_inserting(client):
    """The whole point: a permanently-stuck retry loop stays ONE row."""
    for _ in range(5):
        client.post("/api/events/", _batch(), content_type="application/json")
    assert Event.objects.count() == 1
    assert Event.objects.get().count == 5


def test_coalescing_refreshes_the_summary_and_level(client):
    client.post("/api/events/", _batch(summary="first"), content_type="application/json")
    client.post(
        "/api/events/",
        _batch(summary="still broken", level="warn"),
        content_type="application/json",
    )
    ev = Event.objects.get()
    assert ev.summary == "still broken"
    assert ev.level == "warn"
    assert ev.count == 2


def test_a_blank_key_never_coalesces(client):
    """Two independent actions are two events, not a duplicate — the same
    partial-index exemption `feedback` uses for a blank `source_ref`."""
    for _ in range(3):
        client.post("/api/events/", _batch(key=""), content_type="application/json")
    assert Event.objects.count() == 3


def test_a_different_source_with_the_same_key_is_a_different_row(client):
    client.post("/api/events/", _batch(), content_type="application/json")
    client.post("/api/events/", _batch(source="runner.stream"), content_type="application/json")
    assert Event.objects.count() == 2


def test_a_sourceless_row_is_dropped_not_stored(client):
    """A row nobody can attribute is noise in a pool someone has to read."""
    r = client.post("/api/events/", _batch(source="  "), content_type="application/json")
    assert r.status_code == 200
    assert r.json()["created"] == 0
    assert Event.objects.count() == 0


def test_batch_is_atomic_and_multi(client):
    r = client.post(
        "/api/events/",
        {"items": [_batch()["items"][0], _batch(key="hal@dimagi-ai.com")["items"][0]]},
        content_type="application/json",
    )
    assert r.json()["created"] == 2
    assert Event.objects.count() == 2


# ── reading ──────────────────────────────────────────────────────────────────


def test_list_returns_own_workspace_rows(client):
    client.post("/api/events/", _batch(), content_type="application/json")
    r = client.get("/api/events/")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "gmail.push.missed"
    assert items[0]["count"] == 1


def test_list_hides_another_workspaces_rows(client, user):
    """The tenant FK is NOT NULL precisely so there is no fail-open leg here."""
    other_user = User.objects.create_user("other", "other@dimagi.com", "pw")
    other = Workspace.objects.create(slug="connect", display_name="Connect", created_by=other_user)
    Event.objects.create(workspace=other, source="inbound.gmail", kind="gmail.push", key="x")

    r = client.get("/api/events/")
    assert r.json()["items"] == []


def test_source_and_kind_filter_by_prefix(client):
    client.post("/api/events/", _batch(), content_type="application/json")
    client.post(
        "/api/events/",
        _batch(source="runner.stream", kind="stream.post.failed", key="s1"),
        content_type="application/json",
    )
    assert len(client.get("/api/events/?source=runner").json()["items"]) == 1
    assert len(client.get("/api/events/?source=inbound").json()["items"]) == 1
    assert len(client.get("/api/events/?kind=gmail").json()["items"]) == 1
    assert len(client.get("/api/events/").json()["items"]) == 2


def test_level_filter(client):
    client.post("/api/events/", _batch(), content_type="application/json")
    client.post(
        "/api/events/",
        _batch(level="info", key="ok", kind="gmail.push"),
        content_type="application/json",
    )
    assert len(client.get("/api/events/?level=error").json()["items"]) == 1
    assert len(client.get("/api/events/?level=info").json()["items"]) == 1


def test_since_minutes_filter(client, workspace):
    client.post("/api/events/", _batch(), content_type="application/json")
    Event.objects.update(last_seen_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=2))
    assert client.get("/api/events/?since_minutes=30").json()["items"] == []
    assert len(client.get("/api/events/?since_minutes=300").json()["items"]) == 1


def test_there_is_no_mutation_route(client):
    """A log you can edit is not a record."""
    client.post("/api/events/", _batch(), content_type="application/json")
    ev = Event.objects.get()
    for verb in (client.patch, client.delete, client.put):
        r = verb(f"/api/events/{ev.pk}")
        assert r.status_code in (404, 405), f"{verb.__name__} reached something"
