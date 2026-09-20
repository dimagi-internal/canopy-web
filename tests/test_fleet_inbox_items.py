"""The supervisor's home screen: one call for the whole fleet's open items.

GET /api/items/?state=open aggregates open items across every agent the caller can
see, ranked review -> question then oldest-first. This replaced the old needs_you
projection aggregation (deleted); the inbox is now a pure query over harness.Item.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.agents.models import AgentTask
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership


_EXT = iter(range(1, 10_000))


def _ext() -> str:
    """A unique `ext_id` per task these tests create. Tasks are board cards and
    carry one; an ask raised through the service gets it for free."""
    return f"T{next(_EXT)}"


pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


def _item(agent, title, *, ask_kind=AgentTask.ASK_REVIEW, state="open"):
    """A task carrying an ask, in the given state. `state` is derived on a task
    (`decided_at` + `ask_dismissed`) rather than stored, so it is set here the
    way the verbs would have left it."""
    from django.utils import timezone

    return AgentTask.objects.create(
        agent=agent, ext_id=_ext(), ask_kind=ask_kind, title=title,
        origin=Turn.ORIGIN_API, idempotency_key=f"k-{agent.slug}-{title}",
        decided_at=None if state == "open" else timezone.now(),
        ask_dismissed=(state == "dismissed"),
        status=AgentTask.SUGGESTED if state == "open" else AgentTask.DECLINED,
    )


def test_fleet_inbox_lists_open_items_across_agents(client, workspace):
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=workspace)
    _item(echo, "Draft a story")
    _item(hal, "Sweep security")

    resp = client.get("/api/items/?state=open")
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["agent_slug"] for r in rows} == {"echo", "hal"}
    assert {r["title"] for r in rows} == {"Draft a story", "Sweep security"}


def test_review_items_outrank_question_items(client, workspace):
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    _item(echo, "answer me", ask_kind=AgentTask.ASK_QUESTION)
    _item(echo, "decide me", ask_kind=AgentTask.ASK_REVIEW)

    rows = client.get("/api/items/?state=open").json()
    assert [r["kind"] for r in rows] == ["review", "question"]


def test_decided_and_dismissed_items_are_absent_from_the_inbox(client, workspace):
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=workspace)
    _item(echo, "still open")
    _item(echo, "already decided", state="decided")
    _item(echo, "already dismissed", state="dismissed")

    rows = client.get("/api/items/?state=open").json()
    assert [r["title"] for r in rows] == ["still open"]


def test_fleet_inbox_excludes_other_tenants(client, owner):
    other_owner = User.objects.create_user("other", "other@example.org", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    secret = Agent.objects.create(slug="secret", name="Secret", workspace=other_ws)
    _item(secret, "Classified")

    rows = client.get("/api/items/?state=open").json()
    assert rows == []


def test_fleet_inbox_agrees_with_the_per_agent_items_endpoint(client, workspace):
    """The supervisor's fleet inbox and each agent's own items endpoint must never
    diverge — both must filter tenancy through the same visibility predicate, or one
    is lying and the supervisor is untrustworthy. This pins them equal."""
    alpha = Agent.objects.create(slug="alpha", name="Alpha", workspace=workspace)
    beta = Agent.objects.create(slug="beta", name="Beta", workspace=workspace)
    for i in range(2):
        _item(alpha, f"alpha {i}")
    for i in range(3):
        _item(beta, f"beta {i}")

    fleet = client.get("/api/items/?state=open").json()
    per_agent_total = 0
    for slug in ("alpha", "beta"):
        rows = client.get(f"/api/agents/{slug}/items/?state=open").json()
        fleet_for_slug = [r for r in fleet if r["agent_slug"] == slug]
        assert len(fleet_for_slug) == len(rows)
        per_agent_total += len(rows)
    assert len(fleet) == per_agent_total
