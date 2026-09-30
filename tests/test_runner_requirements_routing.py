"""A conversation's runner requirements (ZDR) are a FLOOR under every routing rung.

A conversation whose host requires `zdr` must never be claimed by a runner that
lacks the `zdr` flag — not through the default list, a source rule, an actor
rule, a strict rule, a pin, a bound session, or the 60s cascade grace — and
there is no fallback. The stuck-turn report and `turn_reach` must agree with
claiming (spec 2026-09-30).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag, Turn
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


def _offline(runner):
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )


def _zdr(runner):
    RunnerFlag.objects.create(runner=runner, flag="zdr")


def _sessions(*runners):
    for r in runners:
        Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
        r.refresh_from_db()


def _zdr_turn(agent, key="c1", **kw):
    s = Session.objects.create(agent=agent, workspace=agent.workspace, title="chat",
                               metadata={"runner_requirements": ["zdr"]})
    return Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=key, routing=Turn.ANY, **kw), s


def test_a_non_zdr_runner_does_not_claim_a_zdr_turn(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    _zdr_turn(a)

    assert services.claim_next_turn(laptop) is None


def test_a_zdr_runner_claims_it(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    turn, _s = _zdr_turn(a)

    claimed = services.claim_next_turn(cloud)

    assert claimed is not None and claimed.pk == turn.pk


def test_a_non_zdr_better_rank_does_not_block_the_zdr_runner(fleet):
    """The laptop is online at rank 0, so without the floor it would block the
    cloud box for the whole grace — and then claim the turn itself. It lacks the
    flag, so it is not in the list at all, and the ZDR box claims at once."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    turn, _s = _zdr_turn(a)  # FRESH: no _age, so the grace has not opened

    claimed = services.claim_next_turn(cloud)

    assert claimed is not None and claimed.pk == turn.pk


def test_an_actor_rule_to_a_non_zdr_box_falls_to_a_zdr_default(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_CANOPY_WEB_CHAT,
        actor="jj@dimagi.com", strict=False,
    )
    turn, _s = _zdr_turn(a, enqueued_by=fleet["user"])

    assert services.claim_next_turn(laptop) is None
    claimed = services.claim_next_turn(cloud)
    assert claimed is not None and claimed.pk == turn.pk


def test_a_strict_rule_to_a_non_zdr_box_waits(fleet):
    """"These runners or nothing" still holds: the floor removes the laptop from
    the strict rung and the truncation keeps the ZDR default out. Nobody claims."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_CANOPY_WEB_CHAT, strict=True,
    )
    _zdr_turn(a)

    assert services.claim_next_turn(laptop) is None
    assert services.claim_next_turn(cloud) is None


def test_a_pin_to_a_non_zdr_runner_does_not_claim(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    _zdr_turn(a, pinned_runner=laptop)

    assert services.claim_next_turn(laptop) is None


def test_a_session_bound_to_a_non_zdr_runner_waits(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _turn, s = _zdr_turn(a)
    RunnerBinding.objects.create(session=s, runner=laptop)

    assert services.claim_next_turn(laptop) is None
    # Stickiness is unchanged: a bound session does not fail over on its own.
    assert services.claim_next_turn(cloud) is None


def test_the_grace_never_promotes_a_non_zdr_runner(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=1)
    _offline(cloud)
    turn, _s = _zdr_turn(a)
    _age(turn)

    assert services.claim_next_turn(laptop) is None


def test_a_turn_without_requirements_is_unaffected(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    s = Session.objects.create(agent=a, workspace=a.workspace, title="chat")
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key="plain", routing=Turn.ANY)

    claimed = services.claim_next_turn(laptop)

    assert claimed is not None and claimed.pk == turn.pk


def test_unclaimable_reports_a_zdr_turn_with_no_zdr_runner_as_config(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    turn, _s = _zdr_turn(a)
    _age(turn)

    stuck = services.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    assert stuck[0]["kind"] == "config" and "ZDR" in stuck[0]["reason"]


def test_unclaimable_reports_an_offline_zdr_runner_as_offline(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _sessions(cloud)
    _zdr(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _offline(cloud)
    turn, _s = _zdr_turn(a)
    _age(turn)

    stuck = services.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    assert stuck[0]["kind"] == "offline"


def test_turn_reach_agrees_with_claiming(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    turn, _s = _zdr_turn(a)

    assert services.turn_reach(turn).kind == services.UNROUTED
    assert services.claim_next_turn(laptop) is None
