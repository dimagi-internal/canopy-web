"""A caller's turn of a stored agent runs only on a cloud runner (canopy-web#2846).

A laptop confines a caller's turn only on the emdash path, and a stored agent has no
emdash project, so the laptop runner fails such a turn at create. The claim never
offers it one, a laptop ranked first does not hold the cloud runner back, and with
no cloud runner the turn waits saying so. Every other turn routes as before.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents import roles
from apps.agents.models import Agent, AgentGrant
from apps.harness import claim
from apps.harness import initiator as _initiator
from apps.harness import services as harness
from apps.harness.models import Runner, RunnerAssignment, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db(transaction=True)

CONFINING = {"signed_in": True, "profiles": 3, "envelope": 2}


def _ctx(*, source="stored", kinds=(Runner.EMDASH, Runner.CLOUD)):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="helper", name="Helper", workspace=ws, source=source,
                                 clis=["claude-code"])
    runners = []
    for rank, kind in enumerate(kinds):
        runner = Runner.objects.create(
            name=f"{kind}-{rank}", kind=kind, host=f"h{rank}", owner=owner, workspace_id=ws.pk,
            status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
            capabilities={"sessions": True, "midturn": 1, "clis": {"claude-code": CONFINING}})
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=rank)
        runners.append(runner)
    return agent, runners


def _callers_turn(agent, key="c1"):
    """A turn that is really confined: a reader's, held to read mode."""
    reader = User.objects.create_user("reader", "reader@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=reader, workspace=agent.workspace, role=WorkspaceMembership.VIEWER)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
    turn, _ = harness.enqueue_turn(initiator=_initiator.for_user(reader, via="test", assurance=_initiator.SESSION),
                                   agent=agent, origin=Turn.ORIGIN_API, idempotency_key=key, prompt="hi")
    assert turn.capability
    return turn


def _scheduled_turn(agent, key="s1"):
    turn, _ = harness.enqueue_turn(initiator=_initiator.system(via="test"), agent=agent,
                                   origin=Turn.ORIGIN_CANOPY_SCHEDULER, idempotency_key=key,
                                   prompt=f"/{agent.slug}:turn")
    assert not turn.capability
    return turn


def test_a_laptop_ranked_first_is_never_offered_it_and_the_cloud_runner_takes_it_at_once():
    agent, (laptop, cloud) = _ctx()
    turn = _callers_turn(agent)
    assert claim.claim_next_turn(laptop) is None
    # Not after the cascade's grace: the laptop is not a better-ranked blocker for this turn.
    assert claim.claim_next_turn(cloud) == turn


def test_a_pin_to_a_laptop_does_not_get_past_it():
    agent, (laptop, _cloud) = _ctx()
    turn = _callers_turn(agent)
    Turn.objects.filter(pk=turn.pk).update(pinned_runner=laptop)
    assert claim.claim_next_turn(laptop) is None


def test_with_only_laptops_it_waits_for_a_cloud_runner():
    agent, (laptop,) = _ctx(kinds=(Runner.EMDASH,))
    turn = _callers_turn(agent)
    assert claim.claim_next_turn(laptop) is None
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    stuck = {row["turn_id"]: row for row in claim.unclaimable_queued_turns(laptop.owner)}
    assert "cloud runner" in stuck[str(turn.pk)]["reason"]
    turn.refresh_from_db()
    assert turn.status == Turn.QUEUED


def test_a_stored_agents_own_turn_still_runs_on_the_laptop_ranked_first():
    agent, (laptop, _cloud) = _ctx()
    turn = _scheduled_turn(agent)
    assert claim.claim_next_turn(laptop) == turn


def test_a_repo_agents_callers_turn_still_runs_on_the_laptop_ranked_first():
    agent, (laptop, cloud) = _ctx(source="repo")
    turn = _callers_turn(agent)
    assert claim.claim_next_turn(cloud) is None  # the laptop ranks first and is available
    assert claim.claim_next_turn(laptop) == turn
