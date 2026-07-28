"""The stuck-turn warning must agree with claiming, per (agent, source).

Before source rules, "can anyone run this?" was a per-agent question. A strict
rule makes it per-source: the cloud box is assigned the agent and online, and
still cannot take an email turn.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, paired_by=jj, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    return {"user": jj, "ws": ws, "agent": echo, "laptop": laptop, "cloud": cloud}


def _age(turn):
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - services.UNCLAIMABLE_GRACE - dt.timedelta(seconds=30)
    )
    return turn


def _stuck_turn(agent, origin, key):
    return _age(Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=key, routing=Turn.ANY
    ))


def _offline(runner):
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )


def test_a_strict_rule_with_an_offline_runner_reads_offline_not_config(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    _offline(laptop)
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    stuck = services.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    # `offline` means "wait, or check the runner"; `config` means "this will never
    # run until you edit routing". A strict rule pointing at a sleeping laptop is
    # the former — the routing is exactly what the operator asked for.
    assert stuck[0]["kind"] == "offline"


def test_an_online_runner_excluded_by_a_strict_rule_does_not_mask_the_stall(fleet):
    """The cloud box is assigned, online and idle — and still cannot take this
    turn. Reporting it as claimable would hide a real stall."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    _offline(laptop)
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.claim_next_turn(cloud) is None          # claiming says no …
    assert len(services.unclaimable_queued_turns(fleet["user"])) == 1   # … so must the warning


def test_a_turn_its_rule_can_run_is_not_reported(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_EMAIL, strict=True
    )
    _stuck_turn(a, Turn.ORIGIN_EMAIL, "k-mail")

    assert services.unclaimable_queued_turns(fleet["user"]) == []


def test_a_bound_session_turn_is_not_reported_when_its_holder_is_not_assigned(fleet):
    """The refinement must mirror the claim loop's bound_to_me short-circuit.

    A session bound to a runner claims on THAT runner regardless of assignments —
    that is what stickiness means. Applying the per-source assignment check to it
    would report a live chat as "no runner is assigned; fix your routing" while
    claiming takes it happily. Surviving runner_target_q is what IDENTIFIES the
    binding holder, so "it was already filtered" is exactly backwards here.
    """
    a, cloud = fleet["agent"], fleet["cloud"]
    # cloud is assigned NOTHING for this agent — only the binding routes to it.
    RunnerAssignment.objects.create(agent=a, runner=fleet["laptop"], rank=0)
    Runner.objects.filter(pk=cloud.pk).update(capabilities={"sessions": True})
    cloud.refresh_from_db()
    session = Session.objects.create(agent=a, workspace=a.workspace, title="chat")
    RunnerBinding.objects.create(session=session, runner=cloud)
    _age(Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key="c1", routing=Turn.ANY,
    ))

    assert services.claim_next_turn(cloud) is not None       # claiming takes it …
    assert services.unclaimable_queued_turns(fleet["user"]) == []  # … so the warning is silent


def test_an_unbound_session_turn_still_follows_its_agents_rules(fleet):
    """No binding yet, so the FIRST send of a new session routes by the agent's
    rules — which is the only send a canopy_web_chat rule ever decides."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    for r in (laptop, cloud):
        Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_CANOPY_WEB_CHAT, strict=True
    )
    _offline(laptop)
    session = Session.objects.create(agent=a, workspace=a.workspace, title="chat")
    _age(Turn.objects.create(
        chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key="c2", routing=Turn.ANY,
    ))

    cloud.refresh_from_db()
    assert services.claim_next_turn(cloud) is None
    assert len(services.unclaimable_queued_turns(fleet["user"])) == 1
