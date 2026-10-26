"""The agent hold — stop every runner from starting ONE agent's turns, and see what tries.

The per-agent sibling of the fleet hold (tests/test_fleet_hold.py): debugging one agent
should not need the whole fleet silenced. The rules these tests hold:

  ONE GATE. `claim_next_turn` refuses the held agent's turns — pins included — and no
  one else's, so it binds every runner without a deploy on any box.

  NOTHING IS LOST. Its turns still enqueue while held and are claimed after the release.

  HONEST STATUS. A turn waiting on the hold says so, not "picking this up".
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture
def ctx():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner,
                                  access_request_domains=[])
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    runner = enrolled(Runner.objects.create(
        name="mbp", kind=Runner.EMDASH, host="mbp", owner=owner, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={},
    ))
    agents = {}
    for slug in ("ace", "hal"):
        agents[slug] = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)
        RunnerAssignment.objects.create(agent=agents[slug], runner=runner, rank=0)
    c = Client()
    c.force_login(owner)
    return {"owner": owner, "ws": ws, "runner": runner, "agents": agents, "client": c}


def _queued(agent, **kw):
    return Turn.objects.create(agent=agent, prompt="do a turn", status=Turn.QUEUED, origin="api",
                               idempotency_key=f"k-{Turn.objects.count()}", **kw)


def _hold(ctx, slug="ace", note="debugging"):
    return ctx["client"].post(f"/api/agents/{slug}/hold", data={"note": note},
                              content_type="application/json")


def test_a_held_agent_is_not_claimed_and_its_turn_waits(ctx):
    turn = _queued(ctx["agents"]["ace"])
    assert _hold(ctx).status_code == 200

    assert claim.claim_next_turn(ctx["runner"]) is None
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_other_agents_keep_running(ctx):
    _queued(ctx["agents"]["ace"])
    hal_turn = _queued(ctx["agents"]["hal"])
    _hold(ctx)

    claimed = claim.claim_next_turn(ctx["runner"])
    assert claimed is not None and claimed.pk == hal_turn.pk


def test_a_pin_does_not_punch_through_the_hold(ctx):
    _queued(ctx["agents"]["ace"], pinned_runner=ctx["runner"])
    _hold(ctx)

    assert claim.claim_next_turn(ctx["runner"]) is None


def test_release_lets_the_backlog_through(ctx):
    turn = _queued(ctx["agents"]["ace"])
    _hold(ctx)
    assert claim.claim_next_turn(ctx["runner"]) is None

    resp = ctx["client"].post("/api/agents/ace/hold/release")

    assert resp.status_code == 200 and resp.json()["held"] is False
    claimed = claim.claim_next_turn(ctx["runner"])
    assert claimed is not None and claimed.pk == turn.pk


def test_hold_reports_who_why_and_what_waits(ctx):
    _queued(ctx["agents"]["ace"])
    _hold(ctx, note="tracing a bad dispatch")

    body = ctx["client"].get("/api/agents/ace/hold").json()

    assert body["held"] is True
    assert body["note"] == "tracing a bad dispatch"
    assert body["held_by_email"] == "jj@dimagi.com"
    assert body["queued"] == 1


def test_holding_again_refreshes_the_note_not_the_start(ctx):
    _hold(ctx, note="first")
    first_at = Agent.objects.get(slug="ace").held_at

    _hold(ctx, note="second")

    ace = Agent.objects.get(slug="ace")
    assert ace.held_note == "second" and ace.held_at == first_at


def test_a_viewer_may_read_but_not_hold(ctx):
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ctx["ws"],
                                       role=WorkspaceMembership.VIEWER)
    c = Client()
    c.force_login(viewer)

    assert c.get("/api/agents/ace/hold").status_code == 200
    assert c.post("/api/agents/ace/hold", data={}, content_type="application/json").status_code == 403
    assert c.post("/api/agents/ace/hold/release").status_code == 403


def test_a_waiting_turn_names_the_hold(ctx):
    turn = _queued(ctx["agents"]["ace"])
    _hold(ctx, note="debugging")

    reach = claim.turn_reach(turn)
    assert reach.kind == claim.UNROUTED and "ace" in reach.reason

    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    stuck = claim.unclaimable_queued_turns(ctx["owner"])
    assert [(s["kind"], s["target"]) for s in stuck] == [("agent_hold", "agent ace")]
    assert "on hold (debugging)" in stuck[0]["reason"]
