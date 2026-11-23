"""A caller's turn of a stored agent goes to any runner the agent's order names, laptops
included, once the laptop's runner makes the agent's checkout its emdash project
(`stored_projects`). An older laptop runner cannot run one and is never handed it.
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


def _ctx(*, source="stored", stored_projects=1, kinds=(Runner.EMDASH, Runner.CLOUD)):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="helper", name="Helper", workspace=ws, source=source,
                                 clis=["claude-code"])
    runners = []
    for rank, kind in enumerate(kinds):
        caps = {"sessions": True, "midturn": 1, "clis": {"claude-code": CONFINING}}
        if kind == Runner.EMDASH:
            caps["stored_projects"] = stored_projects
        runner = Runner.objects.create(
            name=f"{kind}-{rank}", kind=kind, host=f"h{rank}", owner=owner, workspace_id=ws.pk,
            status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities=caps)
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=rank)
        runners.append(runner)
    return agent, runners


def _callers_turn(agent):
    reader = User.objects.create_user("reader", "reader@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=reader, workspace=agent.workspace, role=WorkspaceMembership.VIEWER)
    AgentGrant.objects.create(agent=agent, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
    turn, _ = harness.enqueue_turn(initiator=_initiator.for_user(reader, via="test", assurance=_initiator.SESSION),
                                   agent=agent, origin=Turn.ORIGIN_API, idempotency_key="c1", prompt="hi")
    assert turn.capability
    return turn


def test_a_laptop_ranked_first_runs_a_stored_agents_callers_turn():
    agent, (laptop, cloud) = _ctx()
    turn = _callers_turn(agent)
    assert claim.claim_next_turn(cloud) is None  # the laptop ranks first and is available
    assert claim.claim_next_turn(laptop) == turn


def test_an_older_laptop_runner_is_never_handed_one():
    agent, (laptop, _cloud) = _ctx(stored_projects=0)
    _callers_turn(agent)
    assert claim.claim_next_turn(laptop) is None


def test_with_only_older_laptops_it_waits_saying_what_to_do():
    agent, (laptop,) = _ctx(stored_projects=0, kinds=(Runner.EMDASH,))
    turn = _callers_turn(agent)
    Turn.objects.filter(pk=turn.pk).update(created_at=timezone.now() - claim.UNCLAIMABLE_GRACE * 2)
    stuck = {row["turn_id"]: row for row in claim.unclaimable_queued_turns(laptop.owner)}
    reason = stuck[str(turn.pk)]["reason"]
    assert "update the runner" in reason or "confine" in reason


def test_the_heartbeat_records_it():
    _agent, (laptop, _cloud) = _ctx(stored_projects=0)
    harness.heartbeat(laptop, active_turn_ids=[], stored_projects=1)
    laptop.refresh_from_db()
    assert laptop.capabilities["stored_projects"] == 1
    harness.heartbeat(laptop, active_turn_ids=[])
    laptop.refresh_from_db()
    assert laptop.capabilities["stored_projects"] == 0
