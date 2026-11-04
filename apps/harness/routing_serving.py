"""Which agents a box runs: ONE rule (`runner_routes_agent`, `ServingFacts`, `agents_routed_to`). Import these names from `routing`.
"""
from __future__ import annotations

from django.db.models import Q, prefetch_related_objects

from apps.agents.holding import runner_may_hold_agent
from apps.agents.models import Agent
from apps.agents.standings import Standings

from .models import (
    Runner,
    RunnerAssignment,
    Turn,
)
from .routing_machine import _MACHINE_AGENT_RUNNERS, prime_machine_runners, serves_refusal
from .routing_orders import agents_following_runner, inherited_orders, runner_tenant_slugs, runner_tenant_slugs_of

# ---- which agents a box runs: ONE rule (canopy-web#1563) ----------------------
#
# "Could this box ever claim a turn of this agent?" — the claim's own STATIC gates,
# with the clock and the queue taken out: the box is not retired, the agent is in
# its tenant (enrolled AND its owner may run work there), its owner may hold the
# agent, and the agent routes to it — an ENABLED assignment row (its default list
# or any source/actor rule) or the workspace order it follows. Availability, the
# cascade and a turn's own requirements decide WHICH box takes a given turn; they
# do not decide whether a box runs the agent at all.
#
# The vault accepts a box's device proof by this rule (plus a box holding a live
# turn of the agent, and the owner's own laptop: `device_keys.runner_serves_agent`),
# and a box prepares exactly these agents (`GET /runners/{id}/agents`). Before it,
# the vault counted disabled rows and ignored enrollment and the hold check, and a
# box's refresh walked every agent its token could list — so a box that could
# never claim fizzy asked for fizzy's vault every 30 minutes and its owner was
# emailed each refusal (2026-10-17).

def runner_routes_agent(runner: Runner, agent, facts: ServingFacts | None = None) -> bool:
    """Could `runner` ever claim a turn of `agent`? See the block above. `facts`
    (`ServingFacts`) answers from one read of many runners and agents; without
    it, the facts of this one pair are read."""
    if runner.status == Runner.RETIRED or not runner.owner_id or runner.directed_only():
        return False
    facts = facts or ServingFacts([runner], [agent])
    if serves_refusal(runner, agent.slug):
        return False
    if agent.workspace_id not in facts.tenant_slugs(runner):
        return False
    if not facts.may_hold(runner, agent):
        return False
    if facts.assigned(runner, agent):
        return True
    inh = facts.inherited(agent)
    return bool(inh) and any(r.pk == runner.pk for r in inh.runners)


class ServingFacts:
    """What `runner_routes_agent` and `device_keys.runner_serves_agent` read about
    (runner, agent) pairs, read once for many runners and agents — so a screen
    that asks every pair (each agent's vault on Health asks every runner with a
    device key) pays a fixed number of queries, not a dozen per pair.

    Each fact is read on first use, for every runner and agent given. Read per
    request and dropped with it: not a cache, nothing to invalidate. Asking about
    a runner or agent it was not given raises, rather than answering no."""

    def __init__(self, runners, agents) -> None:
        self.runners = [r for r in runners if r is not None]
        self.agents = list(agents)
        self._by_pk = {r.pk: r for r in self.runners}
        # Each runner's flags (`runner_may_hold_agent`), enrollments
        # (`runner_tenant_slugs_of`) and what each serves (`serves_refusal`) without a query each.
        bare = [r for r in self.runners if "declared_flags" not in getattr(r, "_prefetched_objects_cache", {})]
        if bare:
            prefetch_related_objects(bare, "declared_flags", "enrollments")
        prime_machine_runners([r for r in self.runners if not hasattr(r, _MACHINE_AGENT_RUNNERS)])
        self._tenants: dict | None = None
        self._inherited: dict | None = None
        self._assigned: set | None = None
        self._standings = None
        self._live: set | None = None
        self._once: dict = {}

    def once(self, key, read):
        """`read()` the first time `key` is asked, its answer after — for a
        per-agent fact several checks of one row ask (its vault's members)."""
        if key not in self._once:
            self._once[key] = read()
        return self._once[key]

    def tenant_slugs(self, runner: Runner) -> set[str]:
        if self._tenants is None:
            self._tenants = runner_tenant_slugs_of(self.runners)
        return self._tenants[runner.pk]

    def inherited(self, agent):
        if self._inherited is None:
            self._inherited = inherited_orders([a.id for a in self.agents])
        return self._inherited.get(agent.id)

    def assigned(self, runner: Runner, agent) -> bool:
        """An ENABLED assignment row of `agent` names `runner` (any source)."""
        if self._assigned is None:
            self._assigned = set(
                RunnerAssignment.objects.filter(agent_id__in=[a.id for a in self.agents],
                                                runner_id__in=[r.pk for r in self.runners], enabled=True)
                .values_list("runner_id", "agent_id"))
        return (runner.pk, agent.id) in self._assigned

    def standings(self):
        """Every runner owner's standing with every agent (`access.Standings`)."""
        if self._standings is None:
            self._standings = Standings({r.owner for r in self.runners if r.owner_id}, self.agents)
        return self._standings

    def is_admin(self, owner, agent) -> bool:
        """`agent.is_admin(owner)` for the owner of one of these runners."""
        return self.standings().is_admin(owner, agent)

    def may_hold(self, runner: Runner, agent) -> bool:
        """`runner_may_hold_agent`, with every owner's standing read once."""
        # Its own copy of a runner another list loaded, whose flags it read already.
        return runner_may_hold_agent(self._by_pk.get(runner.pk, runner), agent, standings=self.standings())

    def holds_live_turn(self, runner: Runner, agent) -> bool:
        """`runner` has claimed a live turn of `agent`: one targeting it, or one of
        its sessions."""
        if self._live is None:
            ids = [a.id for a in self.agents]
            rows = (Turn.objects.filter(claimed_by_id__in=[r.pk for r in self.runners],
                                        status__in=(Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN))
                    .filter(Q(agent_id__in=ids) | Q(chat_session__agent_id__in=ids))
                    .values_list("claimed_by_id", "agent_id", "chat_session__agent_id"))
            self._live = {(r, a) for r, direct, via in rows for a in (direct, via) if a is not None}
        return (runner.pk, agent.id) in self._live


def agents_routed_to(runner: Runner):
    """Every agent `runner_routes_agent` holds for, by slug."""
    if runner.status == Runner.RETIRED or not runner.owner_id or runner.directed_only():
        return Agent.objects.none()
    tenant = runner_tenant_slugs(runner)
    if not tenant:
        return Agent.objects.none()
    listed = set(RunnerAssignment.objects.filter(runner=runner, enabled=True)
                 .values_list("agent_id", flat=True))
    candidates = Agent.objects.filter(id__in=listed | agents_following_runner(runner),
                                      workspace_id__in=tenant)
    held = [a.id for a in candidates
            if runner_may_hold_agent(runner, a) and not serves_refusal(runner, a.slug)]
    return Agent.objects.filter(id__in=held).select_related("owner").order_by("slug")
