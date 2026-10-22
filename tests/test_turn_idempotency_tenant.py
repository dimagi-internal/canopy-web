"""An idempotency key replays a turn only to the target it was made for.

`Turn.idempotency_key` is globally unique and the keys canopy mints are
guessable (`sched:<id>:<slot>`, `task-<pk>-<i>`, `checkin-run:<pk>`…). Before
this, `enqueue_turn` returned whatever turn held the key, so an editor in
workspace B could post A's key to `POST /api/harness/turns/` and read A's turn
(prompt, origin_ref, result_note) back in the 200 — or claim a future key so
A's scheduled turn collapsed onto B's.
"""
from __future__ import annotations

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_tenants():
    ws_a = a_workspace("idem-a")
    ws_b = a_workspace("idem-b")
    agent_a = Agent.objects.create(slug="idem-alpha", name="Alpha", workspace=ws_a)
    agent_b = Agent.objects.create(slug="idem-bravo", name="Bravo", workspace=ws_b)
    editor_a = a_member(ws_a, email="idem-a-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    editor_b = a_member(ws_b, email="idem-b-editor@dimagi.com", role=WorkspaceMembership.EDITOR)
    return {"agent_a": agent_a, "agent_b": agent_b, "editor_a": editor_a, "editor_b": editor_b}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _post(client, slug, key, prompt="/x:turn"):
    return client.post(
        "/api/harness/turns/",
        {"agent_slug": slug, "prompt": prompt, "origin": "api", "idempotency_key": key},
        content_type="application/json",
    )


def test_another_tenants_key_is_a_conflict_that_reveals_nothing(two_tenants):
    secret, _ = services.enqueue_turn(
        agent=two_tenants["agent_a"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
        idempotency_key="sched:42:2026-10-22T09:00", prompt="A's private brief",
        origin_ref={"secret": "a-only"},
    )
    res = _post(_as(two_tenants["editor_b"]), "idem-bravo", "sched:42:2026-10-22T09:00")
    assert res.status_code == 409, res.content
    body = res.content.decode()
    assert "A's private brief" not in body
    assert "a-only" not in body
    assert str(secret.pk) not in body
    assert Turn.objects.count() == 1


def test_a_retry_to_the_same_target_replays_the_original(two_tenants):
    client = _as(two_tenants["editor_a"])
    first = _post(client, "idem-alpha", "retry-key", prompt="hello")
    assert first.status_code == 201, first.content
    again = _post(client, "idem-alpha", "retry-key", prompt="hello")
    assert again.status_code == 200, again.content
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["prompt"] == "hello"
    assert Turn.objects.count() == 1


def test_a_preclaimed_key_does_not_swallow_the_real_turn(two_tenants):
    """B posts a future key first; A's own enqueue must not return B's turn."""
    res = _post(_as(two_tenants["editor_b"]), "idem-bravo", "checkin-run:7", prompt="B's bait")
    assert res.status_code == 201, res.content
    with pytest.raises(services.IdempotencyConflict):
        services.enqueue_turn(
            agent=two_tenants["agent_a"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
            idempotency_key="checkin-run:7", prompt="A's check-in",
        )


def test_same_workspace_other_agent_is_a_conflict_too(two_tenants):
    other = Agent.objects.create(slug="idem-alpha-2", name="Alpha 2",
                                 workspace=two_tenants["agent_a"].workspace)
    services.enqueue_turn(agent=other, origin=Turn.ORIGIN_API, idempotency_key="k-other")
    res = _post(_as(two_tenants["editor_a"]), "idem-alpha", "k-other")
    assert res.status_code == 409, res.content
