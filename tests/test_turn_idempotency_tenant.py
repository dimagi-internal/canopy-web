"""An idempotency key is unique per workspace, and replays a turn only to the
target it was made for.

The keys canopy mints are guessable (`sched:<id>:<slot>`, `task-<pk>-<i>`,
`checkin-run:<pk>`…). A key is therefore unique within the turn's workspace
(`Turn.idempotency_scope`), never globally: a member of workspace B can neither
read A's turn back by posting A's key, nor pre-claim one of A's future keys so
that A's own producer finds it taken.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.harness import services
from apps.harness import schedule_turns
from apps.harness.models import AgentSchedule, Turn
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace
from tests.access_helpers import editors_run

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_tenants():
    ws_a = a_workspace("idem-a")
    ws_b = a_workspace("idem-b")
    agent_a = Agent.objects.create(slug="idem-alpha", name="Alpha", workspace=ws_a)
    agent_b = Agent.objects.create(slug="idem-bravo", name="Bravo", workspace=ws_b)
    editors_run(agent_a)
    editors_run(agent_b)
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


def test_another_tenants_key_names_nothing_of_theirs(two_tenants):
    """B posting A's key gets a fresh turn of B's own — A's stays untouched and unseen."""
    secret, _ = services.enqueue_turn(
        agent=two_tenants["agent_a"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
        idempotency_key="sched:42:2026-10-22T09:00", prompt="A's private brief",
        origin_ref={"secret": "a-only"},
    )
    res = _post(_as(two_tenants["editor_b"]), "idem-bravo", "sched:42:2026-10-22T09:00")
    assert res.status_code == 201, res.content
    body = res.content.decode()
    assert "A's private brief" not in body
    assert "a-only" not in body
    assert str(secret.pk) not in body
    assert res.json()["agent_slug"] == "idem-bravo"
    assert Turn.objects.count() == 2


def test_a_retry_to_the_same_target_replays_the_original(two_tenants):
    client = _as(two_tenants["editor_a"])
    first = _post(client, "idem-alpha", "retry-key", prompt="hello")
    assert first.status_code == 201, first.content
    again = _post(client, "idem-alpha", "retry-key", prompt="hello")
    assert again.status_code == 200, again.content
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["prompt"] == "hello"
    assert Turn.objects.count() == 1


def test_a_preclaimed_key_does_not_stop_the_real_turn(two_tenants):
    """B posts A's future key first; A's own enqueue still creates A's turn."""
    res = _post(_as(two_tenants["editor_b"]), "idem-bravo", "checkin-run:7", prompt="B's bait")
    assert res.status_code == 201, res.content
    turn, created = services.enqueue_turn(
        agent=two_tenants["agent_a"], origin=Turn.ORIGIN_CANOPY_SCHEDULER,
        idempotency_key="checkin-run:7", prompt="A's check-in",
    )
    assert created is True
    assert turn.agent == two_tenants["agent_a"]
    assert turn.prompt == "A's check-in"
    assert turn.idempotency_scope == two_tenants["agent_a"].workspace_id
    assert Turn.objects.filter(idempotency_key="checkin-run:7").count() == 2


def test_a_preclaimed_schedule_slot_still_fires(two_tenants):
    """The schedule clock's own `exists()` pre-check looks inside A's workspace,
    so B's turn holding `sched:<id>:<slot>` neither skips the supersede nor the turn."""
    sched = AgentSchedule.objects.create(
        agent=two_tenants["agent_a"], name="Weekly", prompt="/alpha:report",
        cron="0 9 * * 5", timezone="UTC",
    )
    slot = dt.datetime(2026, 10, 23, 9, tzinfo=dt.UTC)
    key = f"sched:{sched.id}:{slot.isoformat()}"
    assert _post(_as(two_tenants["editor_b"]), "idem-bravo", key).status_code == 201
    turn, created = schedule_turns.fire_schedule(sched, slot)
    assert created is True
    assert turn.agent == two_tenants["agent_a"]
    assert turn.prompt == "/alpha:report"


def test_the_database_holds_a_key_unique_per_workspace(two_tenants):
    """The database, not just the pre-check, holds a key unique per workspace."""
    from django.db import IntegrityError, transaction

    services.enqueue_turn(agent=two_tenants["agent_a"], origin=Turn.ORIGIN_API,
                          idempotency_key="dup")
    with pytest.raises(IntegrityError), transaction.atomic():
        Turn.objects.create(agent=two_tenants["agent_a"], origin=Turn.ORIGIN_API,
                            idempotency_key="dup")
    # …while another workspace may hold the same key.
    Turn.objects.create(agent=two_tenants["agent_b"], origin=Turn.ORIGIN_API,
                        idempotency_key="dup")


def test_same_workspace_other_agent_is_a_conflict_too(two_tenants):
    other = Agent.objects.create(slug="idem-alpha-2", name="Alpha 2",
                                 workspace=two_tenants["agent_a"].workspace)
    editors_run(other)
    services.enqueue_turn(agent=other, origin=Turn.ORIGIN_API, idempotency_key="k-other")
    res = _post(_as(two_tenants["editor_a"]), "idem-alpha", "k-other")
    assert res.status_code == 409, res.content
