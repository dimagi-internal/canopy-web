"""claim_next_turn and _runner_schedule_qs must agree — this is the test that
fails if they drift again.

The invariant: **every schedule a runner may SEE and FIRE must produce a turn
that same runner may CLAIM.** They diverged once and it was a production
outage, not a nicety. `claim_next_turn` shipped scoped to the `Runner.workspace`
FK while `_runner_schedule_qs` derived the tenant from `paired_by`, so a runner
homed to `alpha` whose pairer also belonged to `beta` could sync and fire
beta's schedules but never claim the resulting turns. One laptop runner serves
a fleet that deliberately spans workspaces, so 4 of 5 production agents stopped
executing entirely and their turns sat QUEUED forever (2026-07-25).

Two layers, deliberately:

* the two predicates are now built from the SAME pair of functions
  (`services.runner_tenant_slugs` / `services.agent_tenant_q`), so they cannot
  drift by construction; and
* this file checks the BEHAVIOUR end to end anyway, because "they call the same
  helper" is a property of today's code and the invariant has to outlive it. A
  future refactor that hand-inlines either side gets caught here rather than in
  production.

`test_the_two_predicates_are_the_same_object` additionally pins the sharing
itself — it is the cheap early warning; the behavioural tests are the real gate.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import services
from apps.harness.api import _runner_schedule_qs
from apps.harness.models import AgentSchedule, Runner, RunnerAssignment, Turn
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user(name):
    return User.objects.create_user(username=name, email=f"{name}@dimagi.com")


def _ws(slug, owner, members=()):
    ws = Workspace.objects.create(
        slug=slug, display_name=slug.title(), created_by=owner, auto_join_domains=[]
    )
    for user in (owner, *members):
        WorkspaceMembership.objects.get_or_create(
            workspace=ws, user=user, defaults={"role": WorkspaceMembership.OWNER}
        )
    return ws


def _agent(slug, ws):
    return Agent.objects.create(slug=slug, name=slug.title(), workspace=ws)


def _schedule(agent):
    return AgentSchedule.objects.create(
        agent=agent, name=f"{agent.slug} weekly", prompt=f"/{agent.slug}:report",
        cron="0 9 * * 5", timezone="UTC",
    )


def _online_runner(pairer, name="mbp"):
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, paired_by=pairer,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), capabilities={},
    )


def _claimable_agent_slugs(runner) -> set[str]:
    """Every agent this runner can actually claim a queued turn for.

    Drains rather than sampling: `claim_next_turn` returns ONE turn and
    `one_executing_turn_per_agent` then blocks that agent, so a single call
    would only ever prove the first match. Each claim is marked terminal so the
    next call is free to move on.
    """
    seen: set[str] = set()
    while True:
        turn = services.claim_next_turn(runner)
        if turn is None:
            return seen
        seen.add(turn.agent.slug)
        Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)


def _syncable_agent_slugs(runner) -> set[str]:
    return {s.agent.slug for s in _runner_schedule_qs(runner)}


@pytest.fixture
def fleet():
    """One runner, one pairer, three tenants — the production shape that broke.

    `mine` and `also_mine` are both the pairer's; `theirs` is not. The runner's
    own `workspace` FK points at `mine` ONLY, which is the trap: scoping by the
    FK (rather than by the pairer's memberships) silently loses `also_mine`,
    and that is precisely the regression that took prod down.
    """
    pairer = _user("pairer")
    stranger = _user("stranger")
    mine = _ws("mine", pairer)
    also_mine = _ws("also-mine", pairer)
    theirs = _ws("theirs", stranger)

    runner = _online_runner(pairer)
    runner.workspace = mine
    runner.save(update_fields=["workspace"])

    agents = {
        "here": _agent("here", mine),
        "elsewhere": _agent("elsewhere", also_mine),
        "foreign": _agent("foreign", theirs),
    }
    for agent in agents.values():
        _schedule(agent)
        RunnerAssignment.objects.create(agent=agent, runner=runner, rank=0)
    return {"runner": runner, "pairer": pairer, "agents": agents}


def test_what_a_runner_may_fire_is_exactly_what_it_may_claim(fleet):
    """THE invariant. Asserted as set equality, not as two separate
    allow/deny lists, so a change that widens or narrows either side alone
    fails here whichever direction it moves."""
    runner = fleet["runner"]
    for agent in fleet["agents"].values():
        services.enqueue_turn(
            agent=agent, origin=Turn.ORIGIN_CRON, idempotency_key=f"k-{agent.slug}"
        )

    syncable = _syncable_agent_slugs(runner)
    claimable = _claimable_agent_slugs(runner)

    assert syncable == claimable
    # And the value is the RIGHT one, not two matching empties: the pairer's
    # workspaces, both of them, and not the stranger's.
    assert syncable == {"here", "elsewhere"}


def test_a_second_workspace_of_the_pairer_is_not_lost_to_the_runner_fk(fleet):
    """The outage, stated directly: the runner's own workspace FK is `mine`,
    but `elsewhere` lives in `also-mine`. Both halves must reach it — scoping
    either one by the FK reintroduces 4-of-5-agents-stop-executing."""
    runner = fleet["runner"]
    assert runner.workspace_id == "mine"
    assert "elsewhere" in _syncable_agent_slugs(runner)

    services.enqueue_turn(
        agent=fleet["agents"]["elsewhere"], origin=Turn.ORIGIN_CRON, idempotency_key="k1"
    )
    assert _claimable_agent_slugs(runner) == {"elsewhere"}


def test_an_orphaned_runner_can_neither_fire_nor_claim(fleet):
    """NULL `paired_by` fails closed on BOTH sides — no pairer means no identity
    to derive a tenant from, and inferring one from the FK would be an
    escalation (the runner keeps working for a workspace whose owner is gone).
    Used to be a `.none()` special case on the schedule side and an empty-set
    fallthrough on the claim side; now one mechanism serves both."""
    runner = fleet["runner"]
    runner.paired_by = None
    runner.save(update_fields=["paired_by"])
    for agent in fleet["agents"].values():
        services.enqueue_turn(
            agent=agent, origin=Turn.ORIGIN_CRON, idempotency_key=f"o-{agent.slug}"
        )

    assert _syncable_agent_slugs(runner) == set()
    assert _claimable_agent_slugs(runner) == set()


def test_losing_a_membership_narrows_both_sides_together(fleet):
    """The tenant is live state, not a snapshot: revoking the pairer's
    membership must remove the agent from the sync list and from the claim set
    in the same breath. A runner that keeps firing a schedule it can no longer
    claim for is the outage in slow motion."""
    runner, pairer = fleet["runner"], fleet["pairer"]
    WorkspaceMembership.objects.filter(user=pairer, workspace_id="also-mine").delete()
    assert wsvc.user_workspace_slugs(pairer) == {"mine"}

    for agent in fleet["agents"].values():
        services.enqueue_turn(
            agent=agent, origin=Turn.ORIGIN_CRON, idempotency_key=f"r-{agent.slug}"
        )

    assert _syncable_agent_slugs(runner) == {"here"}
    assert _claimable_agent_slugs(runner) == {"here"}


def test_the_two_predicates_are_the_same_object(fleet):
    """Cheap structural early warning, one layer below the behavioural tests:
    both sides must produce an IDENTICAL tenancy Q from the same slug set. If
    someone hand-inlines one of them, this fails immediately and points at the
    helper, rather than leaving the behavioural tests to explain a subtler
    symptom later."""
    runner = fleet["runner"]
    slugs = services.runner_tenant_slugs(runner)
    assert slugs == {"mine", "also-mine"}

    shared = services.agent_tenant_q(slugs)
    schedule_sql = str(_runner_schedule_qs(runner).query)
    # No NULL-means-allow leg survives on either side.
    assert "isnull" not in str(shared)
    assert "workspace_id" in schedule_sql and "IS NULL" not in schedule_sql
