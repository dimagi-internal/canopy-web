"""A conversation's runner requirements (sensitive-data) are a FLOOR under every routing rung.

A conversation whose host requires `sensitive_data` must never be claimed by a runner that
lacks the `sensitive_data` flag — not through the default list, a source rule, an actor
rule, a strict rule, a pin, a bound session, or the 60s cascade grace — and
there is no fallback. The stuck-turn report and `turn_reach` must agree with
claiming (spec 2026-09-30).
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness import claim
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

# A sensitive-data box holds only an agent with a `data: sensitive` capability (dedicated box).
SENSITIVE_IFACE = {"capabilities": {"data": {"callers": ["member"], "sites": ["connect-labs"],
                                       "data": "sensitive", "tools": ["Read"]}}}


def _creator(agent):
    """Whoever is asking about the stuck turn started the chat — a session turn
    is listed only to someone who may read the chat (the chat ACL, not the
    tenant)."""
    from apps.workspaces.models import WorkspaceMembership

    return WorkspaceMembership.objects.filter(
        workspace=agent.workspace, role=WorkspaceMembership.OWNER).first().user


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws,
                                interface=parse(SENSITIVE_IFACE))
    now = timezone.now()
    laptop = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    cloud = Runner.objects.create(
        name="cloud-1", kind=Runner.CLOUD, owner=jj, workspace=ws, status=Runner.ONLINE,
        last_heartbeat_at=now, capabilities={},
    )
    return {"user": jj, "ws": ws, "agent": echo, "laptop": laptop, "cloud": cloud}


def _age(turn):
    Turn.objects.filter(pk=turn.pk).update(
        created_at=timezone.now() - claim.UNCLAIMABLE_GRACE - dt.timedelta(seconds=30)
    )
    return turn


def _offline(runner):
    Runner.objects.filter(pk=runner.pk).update(
        last_heartbeat_at=timezone.now() - dt.timedelta(hours=1)
    )


def _sensitive(runner):
    RunnerFlag.objects.create(runner=runner, flag="sensitive_data")
    _reports_sensitive(runner)


def _sessions(*runners):
    for r in runners:
        Runner.objects.filter(pk=r.pk).update(capabilities={"sessions": True})
        r.refresh_from_db()


def _sensitive_turn(agent, key="c1", **kw):
    s = Session.objects.create(agent=agent, workspace=agent.workspace, title="chat",
                                     created_by=_creator(agent),
                               metadata={"runner_requirements": ["sensitive_data"]})
    return Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key=key, routing=Turn.ANY, **kw), s


def test_a_ordinary_runner_does_not_claim_a_sensitive_turn(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    _sensitive_turn(a)

    assert claim.claim_next_turn(laptop) is None


def test_a_sensitive_runner_claims_it(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    turn, _s = _sensitive_turn(a)

    claimed = claim.claim_next_turn(cloud)

    assert claimed is not None and claimed.pk == turn.pk


def test_a_ordinary_better_rank_does_not_block_the_sensitive_runner(fleet):
    """The laptop is online at rank 0, so without the floor it would block the
    cloud box for the whole grace — and then claim the turn itself. It lacks the
    flag, so it is not in the list at all, and the sensitive-data box claims at once."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=1)
    turn, _s = _sensitive_turn(a)  # FRESH: no _age, so the grace has not opened

    claimed = claim.claim_next_turn(cloud)

    assert claimed is not None and claimed.pk == turn.pk


def test_an_actor_rule_to_a_ordinary_box_falls_to_a_sensitive_default(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_CANOPY_WEB_CHAT,
        actor="jj@dimagi.com", strict=False,
    )
    turn, _s = _sensitive_turn(a, enqueued_by=fleet["user"])

    assert claim.claim_next_turn(laptop) is None
    claimed = claim.claim_next_turn(cloud)
    assert claimed is not None and claimed.pk == turn.pk


def test_a_strict_rule_to_a_ordinary_box_waits(fleet):
    """"These runners or nothing" still holds: the floor removes the laptop from
    the strict rung and the truncation keeps the sensitive-data default out. Nobody claims."""
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(
        agent=a, runner=laptop, rank=0, source=Turn.ORIGIN_CANOPY_WEB_CHAT, strict=True,
    )
    _sensitive_turn(a)

    assert claim.claim_next_turn(laptop) is None
    assert claim.claim_next_turn(cloud) is None


def test_a_pin_to_a_ordinary_runner_does_not_claim(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    _sensitive_turn(a, pinned_runner=laptop)

    assert claim.claim_next_turn(laptop) is None


def test_a_session_bound_to_a_ordinary_runner_waits(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _turn, s = _sensitive_turn(a)
    RunnerBinding.objects.create(session=s, runner=laptop)

    assert claim.claim_next_turn(laptop) is None
    # Stickiness is unchanged: a bound session does not fail over on its own.
    assert claim.claim_next_turn(cloud) is None


def test_the_grace_never_promotes_a_ordinary_runner(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=1)
    _offline(cloud)
    turn, _s = _sensitive_turn(a)
    _age(turn)

    assert claim.claim_next_turn(laptop) is None


def test_a_turn_without_requirements_is_unaffected(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    s = Session.objects.create(agent=a, workspace=a.workspace, title="chat",
                                     created_by=_creator(a))
    turn = Turn.objects.create(chat_session=s, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                               idempotency_key="plain", routing=Turn.ANY)

    claimed = claim.claim_next_turn(laptop)

    assert claimed is not None and claimed.pk == turn.pk


def test_unclaimable_reports_a_sensitive_turn_with_no_sensitive_runner_as_config(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    turn, _s = _sensitive_turn(a)
    _age(turn)

    stuck = claim.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    assert stuck[0]["kind"] == "config" and "sensitive-data" in stuck[0]["reason"]


def test_unclaimable_reports_an_offline_sensitive_runner_as_offline(fleet):
    a, cloud = fleet["agent"], fleet["cloud"]
    _sessions(cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _offline(cloud)
    turn, _s = _sensitive_turn(a)
    _age(turn)

    stuck = claim.unclaimable_queued_turns(fleet["user"])

    assert len(stuck) == 1
    assert stuck[0]["kind"] == "offline"


def test_turn_reach_agrees_with_claiming(fleet):
    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    turn, _s = _sensitive_turn(a)

    assert claim.turn_reach(turn).kind == claim.UNROUTED
    assert claim.claim_next_turn(laptop) is None


# --- sensitive-data is named only when it is the blocker ----------------------------------------


def test_a_sensitive_box_that_cannot_confine_reports_the_profile_reason_not_sensitive(fleet):
    """The sensitive-data box exists and is assigned; what stops the caller's turn is that it
    cannot confine one. Blaming sensitive-data would send the owner to the wrong fix."""
    from apps.harness import turn_status as ts

    a, cloud = fleet["agent"], fleet["cloud"]
    _sessions(cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    turn, _s = _sensitive_turn(a, capability="ask")
    _age(turn)

    [stuck] = claim.unclaimable_queued_turns(fleet["user"])
    assert stuck["kind"] == "config"
    assert "confined" in stuck["reason"] and "sensitive-data" not in stuck["reason"]

    reach = claim.turn_reach(turn)
    assert reach.kind == claim.UNROUTED and not reach.blocked_by_requirements
    assert ts.resolve(turn).requires == ()


def test_no_routing_at_all_reports_the_routing_reason_not_sensitive(fleet):
    a = fleet["agent"]
    turn, _s = _sensitive_turn(a)
    _age(turn)

    [stuck] = claim.unclaimable_queued_turns(fleet["user"])
    assert stuck["kind"] == "config" and "sensitive-data" not in stuck["reason"]
    assert not claim.turn_reach(turn).blocked_by_requirements


def test_a_ordinary_box_that_would_cover_it_names_sensitive_as_the_blocker(fleet):
    from apps.harness import turn_status as ts

    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    turn, _s = _sensitive_turn(a)
    _age(turn)

    [stuck] = claim.unclaimable_queued_turns(fleet["user"])
    assert stuck["kind"] == "config" and "sensitive-data" in stuck["reason"]
    reach = claim.turn_reach(turn)
    assert reach.kind == claim.UNROUTED and reach.blocked_by_requirements
    assert ts.resolve(turn).requires == ("sensitive_data",)


def test_an_offline_sensitive_box_names_sensitive_only_when_a_ordinary_box_is_live(fleet):
    a, laptop, cloud = fleet["agent"], fleet["laptop"], fleet["cloud"]
    _sessions(laptop, cloud)
    _sensitive(cloud)
    RunnerAssignment.objects.create(agent=a, runner=cloud, rank=0)
    _offline(cloud)
    turn, _s = _sensitive_turn(a)
    # Only the sensitive-data box serves it: it is simply offline, and waiting fixes that.
    reach = claim.turn_reach(turn)
    assert reach.kind == claim.OFFLINE and not reach.blocked_by_requirements
    # A live non-sensitive-data box WOULD take it: the requirement is why it waits.
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=1)
    reach = claim.turn_reach(turn)
    assert reach.kind == claim.OFFLINE and reach.blocked_by_requirements


# ---- inheritance (spec 2026-10-22-sensitive-data-agents §3) ------------------


def test_a_turn_started_from_a_sensitive_turn_inherits_its_requirement(fleet):
    """A dispatch, re-ask or transfer from a sensitive conversation has no chat session of
    its own, but carries the conversation's data: same runners."""
    from apps.harness import runner_requirements as rr

    a, laptop = fleet["agent"], fleet["laptop"]
    _sessions(laptop)
    RunnerAssignment.objects.create(agent=a, runner=laptop, rank=0)
    parent, _s = _sensitive_turn(a)
    Turn.objects.filter(pk=parent.pk).update(status=Turn.DONE)
    child = Turn.objects.create(agent=a, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, idempotency_key="child",
                                routing=Turn.ANY, parent_turn=parent)

    assert rr.requirements_of(child) == {"sensitive_data"}
    assert claim.claim_next_turn(laptop) is None


def test_a_turn_started_from_a_sensitive_conversation_inherits_its_requirement(fleet):
    from apps.harness import runner_requirements as rr

    a = fleet["agent"]
    _parent, s = _sensitive_turn(a)
    child = Turn.objects.create(agent=a, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, idempotency_key="child",
                                routing=Turn.ANY, parent_session=s)
    assert rr.requirements_of(child) == {"sensitive_data"}


def test_a_chain_longer_than_the_cap_is_unclaimable_not_unrestricted(fleet):
    from apps.harness import runner_requirements as rr

    a = fleet["agent"]
    node = Turn.objects.create(agent=a, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, idempotency_key="t0",
                               routing=Turn.ANY)
    for i in range(rr.MAX_ANCESTORS + 1):
        node = Turn.objects.create(agent=a, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, idempotency_key=f"t{i + 1}",
                                   routing=Turn.ANY, parent_turn=node)
    assert rr.UNSATISFIABLE in rr.requirements_of(node)


def _reports_sensitive(runner):
    """The box reports sensitive-data mode on its heartbeat: without it a person's `sensitive_data`
    declaration is not in force (runner_requirements.in_force)."""
    from apps.harness.models import Runner as _Runner

    runner.capabilities = {**(runner.capabilities or {}), "data_mode": "sensitive_data"}
    _Runner.objects.filter(pk=runner.pk).update(capabilities=runner.capabilities)
    return runner
