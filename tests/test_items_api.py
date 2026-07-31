"""Items API — authz (404 not 403), batch create, decide-once."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Item, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return User.objects.create_user(username="jj@dimagi.com", email="jj@dimagi.com")


@pytest.fixture
def outsider():
    return User.objects.create_user(username="nope@dimagi.com", email="nope@dimagi.com")


@pytest.fixture
def ada(member):
    ws = Workspace.objects.create(slug="tenant-a", display_name="A", created_by=member)
    WorkspaceMembership.objects.create(workspace=ws, user=member, role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug="ada", name="Ada", workspace=ws)


@pytest.fixture
def client_member(member):
    c = Client()
    c.force_login(member)
    return c


@pytest.fixture
def client_outsider(outsider):
    c = Client()
    c.force_login(outsider)
    return c


def _post_batch(client, slug="ada"):
    return client.post(
        f"/api/agents/{slug}/items/",
        data=[{
            "kind": "review",
            "title": "hal: discard 81 junk/stale unread emails",
            "body": "All 81 are automated or older than 1 week.",
            "origin": "api",
            "batch_key": "fleet-audit-2026-07-14",
            "idempotency_key": "fa-hal-inbox",
            "dispatch": [{"target_agent": "hal", "prompt": "/hal:turn", "origin": "email"}],
        }],
        content_type="application/json",
    )


def test_create_then_list_by_batch(client_member, ada):
    assert _post_batch(client_member).status_code == 201

    rows = client_member.get("/api/agents/ada/items/?batch=fleet-audit-2026-07-14").json()
    assert [r["title"] for r in rows] == ["hal: discard 81 junk/stale unread emails"]
    assert rows[0]["state"] == "open"


def test_create_is_idempotent(client_member, ada):
    _post_batch(client_member)
    _post_batch(client_member)
    assert Item.objects.count() == 1


def test_non_member_gets_404_not_403(client_outsider, ada):
    assert client_outsider.get("/api/agents/ada/items/").status_code == 404
    assert _post_batch(client_outsider).status_code == 404


def test_non_member_cannot_read_or_decide_an_item(client_member, client_outsider, ada):
    _post_batch(client_member)
    item_id = Item.objects.get().id

    assert client_outsider.get(f"/api/items/{item_id}/").status_code == 404
    assert client_outsider.post(
        f"/api/items/{item_id}/decide", data={"decision": "implement"},
        content_type="application/json",
    ).status_code == 404


def test_implement_dispatches_to_the_named_agent(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id

    resp = client_member.post(
        f"/api/items/{item_id}/decide",
        data={"decision": "implement", "comment": "do it"},
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "decided"
    turn = Turn.objects.get()
    assert turn.agent.slug == "hal"
    # Default prompt for a spec that names no prompt, plus the decider's own words
    # appended (dispatch._with_reply) so a steer in the comment reaches the agent.
    assert turn.prompt.startswith("/hal:turn")
    assert "do it" in turn.prompt


def test_deciding_twice_is_409(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id
    body = {"decision": "implement"}
    client_member.post(f"/api/items/{item_id}/decide", data=body, content_type="application/json")

    resp = client_member.post(
        f"/api/items/{item_id}/decide", data=body, content_type="application/json",
    )

    assert resp.status_code == 409
    assert Turn.objects.count() == 1


def test_a_bad_dispatch_spec_is_422_and_leaves_the_item_open(client_member, ada):
    """The API half of the atomicity rule: hal does not exist here, so dispatch
    raises — the decision must roll back and stay retryable, not strand."""
    _post_batch(client_member)
    item_id = Item.objects.get().id

    resp = client_member.post(
        f"/api/items/{item_id}/decide", data={"decision": "implement"},
        content_type="application/json",
    )

    assert resp.status_code == 422
    assert Item.objects.get().state == "open"
    assert Turn.objects.count() == 0


import json as _json


def test_dismiss_persists_an_optional_reason(client_member, ada):
    # A producer retracting its own item can post a reason; it lands on comment and
    # is serialized back — so the board can show "why dismissed".
    _post_batch(client_member)
    item_id = Item.objects.first().id
    resp = client_member.post(
        f"/api/items/{item_id}/dismiss",
        data=_json.dumps({"comment": "retracted: already shipped"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "dismissed"
    assert body["comment"] == "retracted: already shipped"


def test_dismiss_with_no_body_still_works(client_member, ada):
    # Backward compat: the no-body dismiss (the pre-existing call shape) still 200s.
    _post_batch(client_member)
    item_id = Item.objects.first().id
    resp = client_member.post(f"/api/items/{item_id}/dismiss", content_type="application/json")
    assert resp.status_code == 200
    assert resp.json()["state"] == "dismissed"


def test_dismiss_never_dispatches(client_member, ada):
    Agent.objects.create(slug="hal", name="Hal", workspace=ada.workspace)
    _post_batch(client_member)
    item_id = Item.objects.get().id

    resp = client_member.post(f"/api/items/{item_id}/dismiss", content_type="application/json")

    assert resp.status_code == 200
    assert resp.json()["state"] == "dismissed"
    assert Turn.objects.count() == 0


def test_deciding_records_the_deciding_user(client_member, member, ada):
    # A review item so decide (skip) doesn't dispatch; the decider is recorded as a
    # real User FK, and the resolved email is serialized.
    _post_batch(client_member)
    item_id = Item.objects.first().id
    resp = client_member.post(
        f"/api/items/{item_id}/decide",
        data={"decision": "skip", "comment": ""},
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert resp.json()["decided_by_email"] == "jj@dimagi.com"
    assert Item.objects.get(id=item_id).decided_by_user == member
