"""The three cases this feature was built for, end to end through the API.

Deliberately driven through HTTP rather than the service layer: the operator
configures this in the Runners tab, and the thing that must work is the whole
path from that PUT to which runner claims.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet(client):
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={"sessions": True},
    )
    for agent in (ace, echo):
        RunnerAssignment.objects.create(agent=agent, runner=laptop, rank=0)
        RunnerAssignment.objects.create(agent=agent, runner=cloud, rank=1)
    client.force_login(jj)
    return {"client": client, "ace": ace, "echo": echo, "laptop": laptop, "cloud": cloud}


def _rule(client, slug, source, runner, strict):
    res = client.put(
        f"/api/agents/{slug}/runner-rules",
        data={"rules": [{"source": source, "runner_id": str(runner.id), "strict": strict}]},
        content_type="application/json",
    )
    assert res.status_code == 200, res.content


def _queue(agent, origin, key, **kw):
    return Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=key, routing=Turn.ANY, **kw
    )


def test_ace_web_work_runs_on_the_cloud_runner(fleet):
    """Case 1. ace-web delegates execution to canopy-web; that work is why the
    cloud runner exists, so it must not land on the laptop that outranks it."""
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    res = fleet["client"].post(
        "/api/harness/turns/",
        data={"agent_slug": "ace", "origin": "ace_web", "idempotency_key": "e2e-ace",
              "prompt": "/ace:turn", "routing": "any"},
        content_type="application/json",
    )
    assert res.status_code == 201, res.content

    assert services.claim_next_turn(fleet["laptop"]) is None
    claimed = services.claim_next_turn(fleet["cloud"])
    assert claimed is not None and claimed.agent_id == fleet["ace"].id


def test_email_work_stays_on_the_laptop(fleet):
    """Case 2. The inbox watcher enqueues these UNPINNED from whichever box
    polled, so without a rule the cloud box could answer mail the laptop found."""
    _rule(fleet["client"], "echo", "email", fleet["laptop"], True)
    _queue(
        fleet["echo"], Turn.ORIGIN_EMAIL, "email-echo-t1-1",
        origin_ref={"thread_id": "t1", "from": "someone@example.com"},
        prompt="/echo:turn --thread t1",
    )

    assert services.claim_next_turn(fleet["cloud"]) is None
    assert services.claim_next_turn(fleet["laptop"]) is not None


def test_scheduled_work_prefers_the_cloud_but_still_degrades(fleet):
    """Case 3. Non-strict: the lid can be shut at 6am, but a dead cloud box must
    not park the schedule forever."""
    _rule(fleet["client"], "echo", "canopy_scheduler", fleet["cloud"], False)
    _queue(
        fleet["echo"], Turn.ORIGIN_CANOPY_SCHEDULER, "sched:1:2026-07-27T06:00",
        prompt="/echo:turn",
    )

    assert services.claim_next_turn(fleet["laptop"]) is None   # cloud is up, it goes first
    assert services.claim_next_turn(fleet["cloud"]) is not None


def test_a_fall_through_rule_degrades_to_the_laptop_when_the_cloud_is_down(fleet):
    """The other half of case 3 — the reason it is not strict."""
    _rule(fleet["client"], "echo", "canopy_scheduler", fleet["cloud"], False)
    Runner.objects.filter(pk=fleet["cloud"].pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )
    _queue(fleet["echo"], Turn.ORIGIN_CANOPY_SCHEDULER, "sched:1:0700", prompt="/echo:turn")

    assert services.claim_next_turn(fleet["laptop"]) is not None


def test_the_three_rules_coexist_on_one_fleet(fleet):
    fleet["client"].put(
        "/api/agents/echo/runner-rules",
        data={"rules": [
            {"source": "email", "runner_id": str(fleet["laptop"].id), "strict": True},
            {"source": "canopy_scheduler", "runner_id": str(fleet["cloud"].id), "strict": False},
        ]},
        content_type="application/json",
    )
    _rule(fleet["client"], "ace", "ace_web", fleet["cloud"], True)

    _queue(fleet["echo"], Turn.ORIGIN_EMAIL, "m1")
    _queue(fleet["ace"], Turn.ORIGIN_ACE_WEB, "a1")

    first = services.claim_next_turn(fleet["laptop"])
    second = services.claim_next_turn(fleet["cloud"])

    assert first is not None and first.origin == Turn.ORIGIN_EMAIL
    assert second is not None and second.origin == Turn.ORIGIN_ACE_WEB
