"""Directed runner routing — which runner may take which turn.

The per-agent ordered runner lists (`RunnerAssignment`) and the source/actor rules
layered on them, the workspace default order an agent with no runners of its own
follows (`inherited_orders`), the repo-turn order, the tenant gate every
runner-scoped predicate shares (`runner_tenant_slugs` / `agent_tenant_q`), and the
one-executing-turn-per-agent-or-session rule with its mid-turn riders.

Predicates over rows: nothing here writes a turn. `claim.py` composes them into
the claim, and every reader that asks "who would run this?" (the topology, the
turn status, the stuck-turn warning) asks these same functions, so the claim and
its explanations cannot disagree. Split out of `services.py`, which still
re-exports every name here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from django.db.models import Q

from apps.harness import actors
from apps.harness import runner_requirements as rr
from apps.workspaces import services as wsvc

from .models import (
    Runner,
    RunnerAssignment,
    Turn,
    WorkspaceRunnerOrder,
)


def load_assignment_rows(agent_ids) -> tuple[dict, dict]:
    """Load every ENABLED assignment row for these agents in one query, split into
    the two shapes routing needs: the per-agent default list (rank-ordered) and the
    per-(agent, source, actor) RULE — a rank-ordered LIST of rows, not one row
    (spec 2026-09-05).

    enabled=False is filtered here, once, so a disabled row can neither claim nor
    count as a better-ranked availability blocker. A rule whose rows are ALL
    disabled therefore disappears entirely and its turns fall through — meaning
    switching a rule off also switches its strictness off, which is what keeps
    "off" from parking a queue.
    """
    defaults: dict = {}
    priorities: dict = {}
    if not agent_ids:
        return defaults, priorities
    rows = (
        RunnerAssignment.objects.filter(agent_id__in=agent_ids, enabled=True)
        # declared_flags: `assignment_rows_for` reads each runner's flags when a
        # turn carries requirements, and must not query per runner.
        .select_related("runner").prefetch_related("runner__declared_flags").order_by("rank")
    )
    rows = list(rows)
    prime_machine_units([row.runner for row in rows])
    for row in rows:
        if row.source:
            priorities.setdefault((row.agent_id, row.source, row.actor), []).append(row)
        else:
            defaults.setdefault(row.agent_id, []).append((row.rank, row.runner))
    # An agent with no default order of its own follows its workspace's, so
    # every reader of `defaults` — claim, turn status, the unclaimable report,
    # the inbound mailbox routing — sees the inherited list without knowing.
    for agent_id, inh in inherited_orders(agent_ids).items():
        defaults[agent_id] = list(enumerate(inh.runners))
    return defaults, priorities


@dataclass
class InheritedOrder:
    """The default order an agent FOLLOWS because it has none of its own."""

    workspace: str  # the workspace whose order it is (the agent's own, or an ancestor's)
    runners: list  # usable, in rank order
    missing_repo: list  # laptops in the order that do not have the agent's repo
    cannot_hold: list  # boxes whose owner is not one of the agent's admins
    # directed-only boxes in the order (`Runner.directed_only`): take no routed work
    directed: list = field(default_factory=list)


#: `Runner.capabilities` key for the agents a box REPORTS it tried to set up and
#: could not (no clone of the agent on the box): `HeartbeatIn.agents_unprepared`.
UNPREPARED = "unprepared"


def cannot_serve(runner: Runner, slug: str) -> str:
    """Why this box, by its OWN report, cannot run agent `slug` — "" when it has
    not said so. Two reports, each written only by the box itself:

      - a laptop driving emdash names the emdash projects it has (`projects`), and
        an agent's project is its slug. A laptop whose report lacks the slug fails
        the turn at once (`_project_missing` on the runner), so handing it one is
        claiming a turn to fail it (shayoni-win-cdp, fizzy, 2026-10-15). A box
        that has never reported `projects` is UNKNOWN, not "has none".
      - any box names the agents it tried to set up and could not
        (`capabilities["unprepared"]`). A cloud box with no clone of the agent ran
        its turn in a bare scratch dir, with none of its plugin or credentials,
        and the turn ended without doing anything (cloud-ec2-2, fizzy, 2026-10-15).

    A box can only refuse work for itself here, so a self-report is safe to trust
    in this direction. And its isolation unit (`unit_refusal`), which is the same
    kind of self-restriction. Reads the runner row; the unit's machine siblings
    come from `prime_machine_units` when the caller loaded them (one query per
    call otherwise, and none for a box with no machine)."""
    if not slug:
        return ""
    said = unit_refusal(runner, slug)
    if said:
        return said
    caps = runner.capabilities or {}
    if (runner.kind == Runner.EMDASH and runner.engine == Runner.ENGINE_EMDASH
            and "projects" in caps and slug not in runner.project_names()):
        return f"{runner.name} has no emdash project '{slug}'"
    if slug in (caps.get(UNPREPARED) or []):
        return f"{runner.name} could not set '{slug}' up (no clone of it on the box)"
    return ""


# ---- isolation units (spec 2026-10-19 §3) -------------------------------------
#
# A cloud box runs one runner per isolation unit: a workspace tree, or a single
# agent that has a unit of its own. Each unit is its own Unix user with its own
# keys, so an agent's secrets reach only the unit that runs it. Two refusals keep
# routing inside that: an agent unit runs its one agent, and no OTHER runner on
# the same machine (`Runner.machine_id`) runs that agent. Both are restrictions
# a box declares on itself, so trusting its declaration is safe.

#: The attribute `prime_machine_units` caches a runner's machine siblings' agent
#: units on: {agent slug: sibling runner name}.
_MACHINE_UNITS = "_machine_agent_units"


def _agent_unit_slug(unit: dict) -> str | None:
    """The agent an agent unit runs ("" when it names none); None: not an agent unit."""
    if unit.get("kind") != "agent":
        return None
    return str(unit.get("slug") or "")


def prime_machine_units(runners) -> None:
    """Cache, on each of `runners`, the agent units of the OTHER live runners on
    its machine — one query for them all, so the claim and the coverage report
    ask `unit_refusal` of every candidate without a query each."""
    runners = [r for r in runners if r is not None]
    machines = {r.machine_id for r in runners if r.machine_id}
    rows = list(Runner.objects.filter(machine_id__in=machines).exclude(status=Runner.RETIRED)
                .values_list("pk", "name", "machine_id", "capabilities")) if machines else []
    for r in runners:
        units: dict = {}
        for pk, name, machine, caps in rows:
            if machine != r.machine_id or pk == r.pk:
                continue
            unit = (caps or {}).get("unit")
            slug = _agent_unit_slug(unit) if isinstance(unit, dict) else None
            if slug:
                units.setdefault(slug, name)
        setattr(r, _MACHINE_UNITS, units)


def machine_agent_units(runner: Runner) -> dict:
    """{agent slug: runner name} for the agent units on `runner`'s machine other
    than itself — {} for a runner on no known machine."""
    if not runner.machine_id:
        return {}
    if not hasattr(runner, _MACHINE_UNITS):
        prime_machine_units([runner])
    return getattr(runner, _MACHINE_UNITS)


def unit_refusal(runner: Runner, slug: str) -> str:
    """Why `runner`'s isolation unit keeps it from running agent `slug` — "" when
    it does not:

      - it is an AGENT unit, and `slug` is not its agent;
      - another live runner on its machine is the agent unit for `slug`, so the
        agent's secrets stay in that unit.

    Unlike the rest of `cannot_serve`, a pin does not get past this: the claim
    asks it above the pin, and the vault through `runner_routes_agent`."""
    if not slug:
        return ""
    own = _agent_unit_slug(runner.unit())
    if own is not None and slug != own:
        return f"{runner.name} is the agent unit for {own or '(no agent named)'}"
    sibling = machine_agent_units(runner).get(slug)
    if sibling and own != slug:
        return f"{slug} has its own unit on this machine ({sibling})"
    return ""


def turn_unit_refusal(runner: Runner, turn) -> str:
    """`unit_refusal` for a whole turn: an agent unit refuses every turn that does
    not run AS its agent — another agent's, and one with no acting agent at all
    (a repo turn, an agentless chat) — and any runner refuses a turn whose agent
    has its own unit elsewhere on its machine. "" when it takes the turn."""
    agent = turn.acting_agent
    own = _agent_unit_slug(runner.unit())
    if own is not None and (agent is None or agent.slug != own):
        return f"{runner.name} is the agent unit for {own or '(no agent named)'}"
    return unit_refusal(runner, agent.slug) if agent is not None else ""


def agents_following_runner(runner: Runner, among=None) -> set:
    """Agents that reach `runner` through an INHERITED order: no order of their
    own, and the order they follow lists it as usable for them. The claim's
    target filter joins assignment rows, which an inheriting agent has none of.

    `among` narrows the question to those agent ids — the claim passes the agents
    with queued work, so an idle poll (the common case, every box every few
    seconds) asks it of nobody rather than of the whole tree."""
    from apps.agents.models import Agent

    if among is not None and not among:
        return set()
    if runner.directed_only():
        return set()  # takes no routed work, an inherited order's included
    listed = set(WorkspaceRunnerOrder.objects.filter(runner=runner, enabled=True)
                 .values_list("workspace_id", flat=True))
    if not listed:
        return set()
    scope = listed | wsvc.descendant_slugs(listed)
    agents = Agent.objects.filter(workspace_id__in=scope)
    if among is not None:
        agents = agents.filter(id__in=among)
    candidates = set(agents.values_list("id", flat=True))
    return {aid for aid, inh in inherited_orders(candidates).items()
            if any(r.id == runner.id for r in inh.runners)}


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

def runner_routes_agent(runner: Runner, agent) -> bool:
    """Could `runner` ever claim a turn of `agent`? See the block above."""
    from apps.agents.services import runner_may_hold_agent

    if runner.status == Runner.RETIRED or not runner.owner_id or runner.directed_only():
        return False
    if unit_refusal(runner, agent.slug):
        return False
    if agent.workspace_id not in runner_tenant_slugs(runner):
        return False
    if not runner_may_hold_agent(runner, agent):
        return False
    if RunnerAssignment.objects.filter(agent=agent, runner=runner, enabled=True).exists():
        return True
    inh = inherited_orders([agent.id]).get(agent.id)
    return bool(inh) and any(r.pk == runner.pk for r in inh.runners)


def agents_routed_to(runner: Runner):
    """Every agent `runner_routes_agent` holds for, by slug."""
    from apps.agents.models import Agent
    from apps.agents.services import runner_may_hold_agent

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
            if runner_may_hold_agent(runner, a) and not unit_refusal(runner, a.slug)]
    return Agent.objects.filter(id__in=held).select_related("owner").order_by("slug")


def agents_served_by(runner: Runner):
    """The agents this box runs — `agents_routed_to`, the name the readiness
    checks and the fleet view already import."""
    return agents_routed_to(runner)


def default_order_source(agent) -> str | None:
    """The workspace whose default order this agent would follow: its own
    workspace or the nearest above it with an enabled order. None: none has one."""
    if not agent.workspace_id:
        return None
    chain = [agent.workspace_id, *agent.workspace.ancestor_slugs()]
    have = set(WorkspaceRunnerOrder.objects.filter(workspace_id__in=chain, enabled=True)
               .values_list("workspace_id", flat=True))
    return next((s for s in chain if s in have), None)


def _ancestor_chains(slugs) -> dict:
    """{slug: [slug, parent, grandparent, …]} from ONE read of the workspace tree
    (a small table), instead of a query per level per workspace."""
    from apps.workspaces.models import MAX_DEPTH, Workspace

    parent = dict(Workspace.objects.values_list("slug", "parent_id"))
    out: dict = {}
    for slug in slugs:
        chain, seen, cur = [slug], {slug}, parent.get(slug)
        while cur and cur not in seen and len(chain) <= MAX_DEPTH:
            chain.append(cur)
            seen.add(cur)
            cur = parent.get(cur)
        out[slug] = chain
    return out


def agents_with_own_order(agent_ids) -> set:
    """Agents that have a default order of their own — ANY default row, enabled
    or not. An owner who switched every runner off has still chosen a list;
    falling back to the workspace's would route work they parked."""
    return set(
        RunnerAssignment.objects.filter(agent_id__in=agent_ids, source="")
        .values_list("agent_id", flat=True).distinct()
    )


def inherited_orders(agent_ids) -> dict:
    """{agent_id: InheritedOrder} for every agent here WITHOUT an order of its
    own, whose workspace (or nearest ancestor) has one.

    The order is followed live, never copied: replacing a cloud box means
    editing one workspace's list, not every agent (2026-10-03).

    Three kinds of listed runner are dropped for an agent rather than kept — kept,
    each would read as an available better rank and stall the agent behind a box
    that can never take its work, until the cascade grace:
      - a directed-only box (`Runner.directed_only`), which takes no routed work.
      - a laptop that does not have the agent's repo (it reports the emdash
        projects it has, and an agent's project is its slug). A cloud runner sets
        agents up itself and is never dropped for this.
      - a box whose owner is not one of the agent's admins
        (`runner_may_hold_agent`) — the claim would refuse it anyway.
    Both are returned so a screen can say why."""
    from apps.agents.models import Agent

    agent_ids = set(agent_ids or ())
    if not agent_ids:
        return {}
    own = agents_with_own_order(agent_ids)
    agents = list(Agent.objects.filter(id__in=agent_ids - own).select_related("workspace"))
    if not agents:
        return {}
    chains = _ancestor_chains({a.workspace_id for a in agents if a.workspace_id})
    orders = load_workspace_orders({s for chain in chains.values() for s in chain})
    held: dict = {}  # (agent_id, owner_id) -> bool; owners are few

    def may_hold(runner, agent) -> bool:
        key = (agent.id, runner.owner_id)
        if key not in held:
            from apps.agents.services import runner_may_hold_agent

            held[key] = runner_may_hold_agent(runner, agent)
        return held[key]

    out: dict = {}
    for agent in agents:
        source = next((s for s in chains.get(agent.workspace_id, []) if orders.get(s)), None)
        if source is None:
            continue
        inh = InheritedOrder(workspace=source, runners=[], missing_repo=[], cannot_hold=[])
        for r in orders[source]:
            if r.directed_only():
                inh.directed.append(r)
            elif r.kind == Runner.EMDASH and agent.slug not in r.project_names():
                inh.missing_repo.append(r)
            elif not may_hold(r, agent):
                inh.cannot_hold.append(r)
            elif unit_refusal(r, agent.slug):
                continue  # another agent's unit, or the agent has its own: not for it
            else:
                inh.runners.append(r)
        out[agent.id] = inh
    return out


def assignment_rows_for(
    agent_id, origin: str, actor: str, defaults: dict, priorities: dict,
    *, requires: frozenset = frozenset(), serves: str = "",
) -> list:
    """THE ordered runner list for one (agent, source, actor) triple — what the
    availability cascade then walks. Pure: no queries, no clock.

    The ladder is actor rule, then source rule (`actor=""`), then the default
    order; `claim_next_turn` layers pins and session stickiness above it.

    Ranks are renumbered from 0 because the cascade compares them to decide who
    blocks whom; stored ranks are per-rule and would otherwise put two runners at
    rank 0, each apparently blocking the other.

    `requires` is the conversation's runner requirements (`runner_requirements`):
    a runner lacking any of them is dropped BEFORE ranks are renumbered.

    `serves` is the agent's slug: a runner that reports it cannot run that agent
    (`cannot_serve`) is dropped the same way, so it neither claims a turn only to
    fail it nor counts as a better-ranked blocker. Empty: not asked.

    A directed-only box (`Runner.directed_only`) is always dropped, after the
    strict truncation: it takes no routed work, so a strict rule naming only it
    leaves the turn waiting, visibly.
    """
    base = defaults.get(agent_id) or []
    exact = priorities.get((agent_id, origin, actor)) if actor else None
    anyone = priorities.get((agent_id, origin, ""))
    ladder = [rule for rule in (exact, anyone) if rule]
    if not ladder:
        out = [r for _rank, r in base]
    else:
        seen: set = set()
        out = []
        truncated = False
        for rule in ladder:
            for row in rule:  # already rank-ordered by load_assignment_rows
                if row.runner_id not in seen:
                    seen.add(row.runner_id)
                    out.append(row.runner)
            # "These runners or nothing": the turn waits rather than degrading.
            # Everything below this rung is absent from the list, so the
            # wedged-runner grace has nobody to promote — which is what makes "and
            # nowhere else" actually hold. Appending the default order regardless
            # would hand the work straight back to the runners the rule exists to
            # exclude.
            if rule[0].strict:
                truncated = True
                break
        if not truncated:
            out += [r for _rank, r in base if r.id not in seen]
    if requires:
        # A requirement is a FLOOR, applied to the composed list so a runner that
        # lacks it neither claims nor counts as a better-ranked blocker, and the
        # grace has nobody below the floor to promote (spec 2026-09-30). Applied
        # AFTER the strict truncation, so a strict rule whose runners all lack the
        # flag leaves an empty list — the turn waits, it does not fall through.
        out = [r for r in out if rr.satisfies(r.flags, requires)]
    out = [r for r in out if not r.directed_only()]
    if serves:
        # The same FLOOR, after the strict truncation for the same reason: a strict
        # rule whose every box reports it cannot run the agent leaves an empty list,
        # and the turn waits (visibly: `unclaimable_queued_turns`), never falls
        # through to boxes the rule exists to exclude.
        out = [r for r in out if not cannot_serve(r, serves)]
    return list(enumerate(out))


def _kind_allows(runner: Runner, routing: str) -> bool:
    if routing == Turn.LOCAL_ONLY:
        return runner.kind in (Runner.EMDASH, Runner.REMOTE)
    return True


# Rank = availability cascade (spec 2026-07-24-directed-runner-routing). A lower
# rank may claim only while every better rank is unavailable — EXCEPT after the
# grace: an online-but-wedged runner (heartbeating, never claiming) must not
# stall an agent's queue forever, so a turn queued past the grace opens to the
# next assigned rank regardless of upstream availability.
CASCADE_GRACE_SECONDS = 60


def _assignment_allows_for_agent(runner: Runner, agent_id, turn: Turn,
                                 defaults: dict, priorities: dict, now) -> bool:
    """False when this runner is not in the agent's list FOR THIS TURN'S SOURCE;
    True when it is and either every better rank is unavailable or the turn has
    aged past the grace.

    The list is composed per (agent, origin) — a strict source rule yields a
    single-entry list, so every other runner reads as "not in the list" and the
    grace has nobody to promote.
    """
    rows = assignment_rows_for(
        agent_id, turn.origin, actors.actor_of(turn), defaults, priorities,
        requires=rr.requirements_of(turn), serves=routed_slug(turn, agent_id),
    )
    mine = next((rank for rank, r in rows if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for rank, r in rows if rank < mine)


def routed_slug(turn: Turn, agent_id) -> str:
    """The slug of `agent_id` — the agent whose list routes `turn` (its own, or
    its chat's). Read off the rows the claim already select_related, no query."""
    if turn.agent_id and turn.agent_id == agent_id:
        return turn.agent.slug
    if turn.chat_session_id and turn.chat_session.agent_id == agent_id:
        return turn.chat_session.agent.slug
    return ""


def _assignment_allows(runner: Runner, turn: Turn, defaults: dict, priorities: dict, now) -> bool:
    return _assignment_allows_for_agent(runner, turn.agent_id, turn, defaults, priorities, now)


def _is_repo_turn(turn: Turn) -> bool:
    """A project dispatch: no agent, no chat session. The one turn kind the
    workspace runner order routes."""
    return not turn.agent_id and not turn.chat_session_id and bool(turn.project)


def load_workspace_orders(ws_ids) -> dict:
    """{workspace_id: [runner, …] in rank order} — ENABLED rows only, one query.

    A workspace absent from the result has no order, and its repo turns route as
    they always have (any runner that declares the repo). Filtering disabled rows
    here, once, keeps a disabled runner from claiming or blocking — the same rule
    `load_assignment_rows` applies to an agent's list."""
    out: dict = {}
    ws_ids = {w for w in ws_ids if w}
    if not ws_ids:
        return out
    rows = (
        WorkspaceRunnerOrder.objects.filter(workspace_id__in=ws_ids, enabled=True)
        .select_related("runner").prefetch_related("runner__declared_flags")
        .exclude(runner__status=Runner.RETIRED).order_by("rank")
    )
    rows = list(rows)
    prime_machine_units([row.runner for row in rows])
    for row in rows:
        out.setdefault(row.workspace_id, []).append(row.runner)
    return out


def order_for(workspace_id, project: str, orders: dict, *, requires: frozenset,
              sessions: bool = False) -> list | None:
    """THE ordered runner list a workspace's runner order gives one piece of
    repo work — a repo turn or an unbound project chat — or None when that
    workspace has no order. Pure: no queries, no clock.

    Only runners that DECLARE the repo (when there is one) and meet the work's
    requirements are kept, and ranks are renumbered over what is left — a
    better-ranked runner that could never take this work must not count as a
    blocker for one that can. A directed-only box takes no routed work, so it is
    never in the list (`Runner.directed_only`). `sessions`: the work is a chat
    turn, so only a session-capable box can take it — kept, any other would read
    as an available better rank and hold back the box that can, until the grace."""
    listed = orders.get(workspace_id)
    if not listed:
        return None
    return [r for r in listed
            if not r.directed_only()
            and (not sessions or r.session_capable())
            and (not project or project in r.project_names())
            and rr.satisfies(r.flags, requires)]


def repo_order_for(turn: Turn, orders: dict, *, requires: frozenset | None = None) -> list | None:
    """`order_for` for one repo turn (its own workspace and repo)."""
    reqs = rr.requirements_of(turn) if requires is None else requires
    return order_for(turn.workspace_id, turn.project, orders, requires=reqs)


def _cascade_allows(runner: Runner, rows: list, turn: Turn, now) -> bool:
    """The availability cascade over a workspace order: this runner is listed,
    and either every better rank is unavailable or the turn has aged past the
    grace — `_assignment_allows_for_agent`'s rule over an order."""
    mine = next((i for i, r in enumerate(rows) if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for r in rows[:mine])


def _repo_order_allows(runner: Runner, turn: Turn, orders: dict, now) -> bool:
    """The availability cascade for a repo turn — `_assignment_allows_for_agent`
    over the workspace order instead of an agent's list, with the same grace."""
    rows = repo_order_for(turn, orders)
    if rows is None:
        return True
    return _cascade_allows(runner, rows, turn, now)


# ---- project chats: a chat with no agent, only a repo (spec 2026-10-19 #6) ----
#
# Its UNBOUND turns follow the rules a repo turn follows, plus one: with no
# workspace order, only a box the chatting person administers. Before, any
# session-capable box in the tenant took them, so a teammate's new project chat
# landed on someone else's laptop. A bound session still claims only on its
# holder, and a pin still bypasses all of this. The claim asks
# `project_session_allows`; every reader that mirrors it (`claim._refined_allows`,
# so turn_reach, the unclaimable report and the topology) asks
# `project_session_covers` — the same predicates without the clock.

def is_project_session_turn(turn: Turn) -> bool:
    """A chat turn with no agent whose session is not bound to a runner — the
    turns the project-chat rules route. A chat with neither an agent nor a repo
    counts: it has no repo to match, but it is still somebody's work."""
    if not turn.chat_session_id or turn.chat_session.agent_id:
        return False
    binding = getattr(turn.chat_session, "runner_binding", None)
    return binding is None or binding.runner_id is None


def project_session_rows(turn: Turn, orders: dict, *, requires: frozenset | None = None) -> list | None:
    """The workspace order a project chat's turn follows (`order_for` on its
    session's workspace and repo), or None when the workspace has none."""
    sess = turn.chat_session
    reqs = rr.requirements_of(turn) if requires is None else requires
    return order_for(sess.workspace_id, sess.project, orders, requires=reqs, sessions=True)


def _chatter_administers(runner: Runner, turn: Turn, admins: dict | None) -> bool:
    """Rule 3: the person who sent it (`enqueued_by`) administers this box. A
    turn with no person behind it gets no box this way. Cached per (box, person)
    when the caller passes `admins`."""
    if not turn.enqueued_by_id:
        return False
    from .runners import can_administer_runner

    if admins is None:
        return can_administer_runner(turn.enqueued_by, runner)
    key = (runner.id, turn.enqueued_by_id)
    if key not in admins:
        admins[key] = can_administer_runner(turn.enqueued_by, runner)
    return admins[key]


def project_session_covers(runner: Runner, turn: Turn, orders: dict, *,
                           requires: frozenset | None = None, admins: dict | None = None) -> bool:
    """Could this runner EVER take this unbound project-chat turn? Rules 1–3,
    without the cascade's clock (membership, as for a repo turn's order)."""
    if runner.directed_only():
        return False
    project = turn.chat_session.project
    if project and project not in runner.project_names():
        return False  # 1. the box declares the repo
    rows = project_session_rows(turn, orders, requires=requires)
    if rows is not None:
        return any(r.id == runner.id for r in rows)  # 2. the workspace shares it
    return _chatter_administers(runner, turn, admins)  # 3. or you run it


def project_session_allows(runner: Runner, turn: Turn, orders: dict, now, *,
                           admins: dict | None = None) -> bool:
    """`project_session_covers`, then the order's availability cascade and grace."""
    if not project_session_covers(runner, turn, orders, admins=admins):
        return False
    rows = project_session_rows(turn, orders)
    return rows is None or _cascade_allows(runner, rows, turn, now)


def project_session_waits_why(turn: Turn, orders: dict) -> str:
    """Why no box will take this unbound project-chat turn, in the words of the
    rules above — the reason `unclaimable_queued_turns` and `turn_reach` give."""
    sess = turn.chat_session
    ws = sess.workspace_id
    repo = sess.project
    with_repo = f"with {repo}" if repo else "that takes chats"
    if project_session_rows(turn, orders) is not None:
        return f"the runner order in {ws} lists no session-capable box {with_repo}"
    if not turn.enqueued_by_id:
        return (f"no runner order in {ws}, and this project chat has no person behind "
                "it, so it needs one")
    who = turn.enqueued_by.email or turn.enqueued_by.get_username()
    return f"no runner order in {ws}, and {who} administers no box {with_repo}"


EXECUTING = [Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]


def busy_agent_ids():
    """Agents with an executing turn — `claim_next_turn` claims none of their
    queued turns. A subquery, not a list.

    `agent__isnull=False` is load-bearing: a PROJECT turn has agent_id NULL, so
    without it one executing repo turn injected a NULL into the IN-list and
    every queued AGENT turn evaluated `agent_id IN (…, NULL)` -> NULL -> got
    wrongly excluded. Not "that agent is busy": the runner claimed NOTHING AT
    ALL while any project turn ran. Observed on the cloud runner — a drill sat
    QUEUED and pinned for 40+ minutes while the runner was online and
    heartbeating and POST /claim returned 204, because two canopy-web project
    turns happened to be executing.
    """
    # A LOCAL turn (a person's own session on their private runner) never holds the
    # agent: it is excluded from `one_executing_turn_per_agent` for the same reason.
    return (Turn.objects.filter(status__in=EXECUTING, agent__isnull=False)
            .exclude(origin=Turn.ORIGIN_LOCAL).values("agent_id"))


def busy_session_ids():
    """Chat sessions with an executing turn. A session serializes like an agent:
    never claim a session that already has an executing turn
    (one_executing_turn_per_session would reject the claim anyway).

    `chat_session__isnull=False` is load-bearing for the same reason as
    `busy_agent_ids`: executing agent/project turns (chat_session_id NULL) would
    inject a NULL into the IN-list, and every queued SESSION turn would then
    evaluate `id IN (…, NULL)` -> NULL -> get wrongly excluded whenever any
    agent turn is running.
    """
    return Turn.objects.filter(status__in=EXECUTING, chat_session__isnull=False,
                               rides_turn__isnull=True).values("chat_session_id")


def delivers_midturn(runner: Runner | None) -> bool:
    """This runner's code types a follow-up into a running turn (reported on its
    heartbeat as `midturn`). The cloud runner does not, and keeps queueing."""
    return runner is not None and bool(int(runner.capabilities.get("midturn") or 0))


def _dialog_up(session_id) -> bool:
    """A parsed dialog (one with options) is on this session's screen. Claude
    Code draws it where the composer would be, so a follow-up typed now bounces
    — it waits for the dialog to be answered, as a person's typing would. An
    option-less notification marker does not count: nothing was parsed to
    produce it (the composer's own rule, `menuBlocksComposer`)."""
    from apps.canopy_sessions.models import RunnerBinding

    menu = (RunnerBinding.objects.filter(session_id=session_id)
            .values_list("pending_question", flat=True).first())
    return bool(isinstance(menu, dict) and menu.get("options"))


#: `origin_ref` flag on a follow-up whose mid-turn delivery failed: it waits for
#: the running turn to end, as before canopy-web#1153, rather than ride again.
MIDTURN_FAILED = "midturn_failed"


def may_ride(turn: Turn, holder: Turn) -> bool:
    """Can queued `turn` be delivered INTO `holder`, its conversation's running
    turn, instead of waiting for it to end (canopy-web#1153)?

    The one rule both `claim_next_turn` and `blocking_turn` ask, so "queued
    behind" and "not claimable" still cannot disagree. Only full-profile turns
    on both sides: a confined turn runs in its own `cx-` session, so it has no
    business in the owner's, and the owner's has none in a caller's.
    """
    return (turn.chat_session_id is not None
            and not (turn.origin_ref or {}).get(MIDTURN_FAILED)
            and holder.chat_session_id == turn.chat_session_id
            and holder.status in (Turn.CLAIMED, Turn.RUNNING)
            and holder.rides_turn_id is None
            and not turn.capability and not holder.capability
            and turn.pinned_runner_id in (None, holder.claimed_by_id)
            and holder.claimed_by_id is not None
            and delivers_midturn(holder.claimed_by)
            and not _dialog_up(turn.chat_session_id))


def ridable_sessions(runner: Runner) -> dict:
    """{chat_session_id: running turn id} for conversations this runner is
    running now and can deliver a follow-up into."""
    if not delivers_midturn(runner):
        return {}
    held = Turn.objects.filter(
        status__in=[Turn.CLAIMED, Turn.RUNNING], claimed_by=runner, chat_session__isnull=False,
        rides_turn__isnull=True, capability="",
    ).values_list("chat_session_id", "pk")
    return {sid: pk for sid, pk in held if not _dialog_up(sid)}


def blocking_turn(turn: Turn) -> Turn | None:
    """The executing turn this QUEUED turn is waiting behind, or None.

    The question `claim_next_turn` answers with `busy_session_ids` /
    `busy_agent_ids`, asked of one turn — so "queued behind the current turn"
    in the status line and "not claimable" in the claim cannot drift apart
    (`tests/test_turn_status.py` pins the two together). Its session's executing
    turn first: that is the one a person can see ("the previous message in this
    thread").
    """
    if turn.status != Turn.QUEUED:
        return None
    executing = Turn.objects.filter(status__in=EXECUTING).select_related("claimed_by")
    if turn.chat_session_id:
        held = executing.filter(chat_session_id=turn.chat_session_id,
                                rides_turn__isnull=True).first()
        if held is not None:
            # Not blocked when it can be delivered into the running turn: the
            # runner holding it will claim it on its next tick.
            return None if may_ride(turn, held) else held
    if turn.agent_id:
        return executing.filter(agent_id=turn.agent_id).exclude(origin=Turn.ORIGIN_LOCAL).first()
    return None
    qs = Turn.objects.select_related("claimed_by")
    if turn.chat_session_id and Turn.objects.filter(
            pk=turn.pk, chat_session_id__in=busy_session_ids()).exists():
        return qs.filter(status__in=EXECUTING, chat_session_id=turn.chat_session_id).first()
    if turn.agent_id and Turn.objects.filter(pk=turn.pk, agent_id__in=busy_agent_ids()).exists():
        return qs.filter(status__in=EXECUTING, agent_id=turn.agent_id).first()
    return None


def runner_target_q(runner: Runner, exclude_slugs: list[str] | None = None,
                    *, ignore_unit: bool = False) -> Q:
    """Which queued turns this runner can TARGET under directed routing (spec
    2026-07-24): agents via RunnerAssignment (the one source of truth —
    capabilities.agents no longer routes agent turns), repos via
    capabilities.projects, and sessions (when session-capable) with binding
    STICKINESS — a bound session matches only its binding holder; a bound
    session whose holder is gone matches nobody until the user places it.

    Shared by claim_next_turn and unclaimable_queued_turns so the "can anyone run
    this?" warning can never disagree with what claiming actually does — the same
    class of drift that made REST and the WebSocket show different transcripts.
    The claim path layers routing_q, the pin arm, the availability cascade, and
    the per-candidate refinements on top; this is the coarse target match.

    A directed-only box (`Runner.directed_only`) targets only the sessions bound
    to it; the claim adds the turns pinned to it. Nothing routed reaches it.
    """
    if runner.directed_only():
        return Q(chat_session__isnull=False, chat_session__runner_binding__runner=runner)
    # Both conditions on the SAME assignment row (a single Q, not two ANDed Qs)
    # so they share the join — a disabled row for this runner must not match
    # via some OTHER enabled row on the same agent.
    agent_leg = Q(agent__runner_assignments__runner=runner, agent__runner_assignments__enabled=True)
    # Only agents with queued work can be targeted, so only they need asking.
    queued_agents = set(Turn.objects.filter(status=Turn.QUEUED, agent__isnull=False)
                        .values_list("agent_id", flat=True).distinct())
    following = agents_following_runner(runner, among=queued_agents)
    if following:
        agent_leg |= Q(agent_id__in=following)
    if exclude_slugs:
        # Per-agent pause: the runner locally paused these agents; never claim their
        # queued turns (they stay QUEUED, resumed the moment the pause is lifted).
        # Scoped to agents by name and by nature — pausing an agent says nothing
        # about a repo, so project turns keep flowing.
        agent_leg &= ~Q(agent__slug__in=list(exclude_slugs))
    q = agent_leg | Q(project__in=runner.project_names())
    if runner.session_capable():
        # Stickiness: a bound session's turns go to the binding holder ONLY. A
        # bound session whose holder is gone claims NOWHERE until the user places
        # it (chat offers wait/continue). Unbound sessions are open here and
        # refined per-candidate in the claim loop (agent sessions follow the
        # assignment cascade; project sessions `project_session_allows`).
        #
        # Deliberately NOT relaxed to "stale bindings fail over automatically"
        # (spec 2026-07-24): continuing elsewhere means a FRESH emdash session with
        # none of the conversation's warm context, so it is the user's call, not a
        # timeout's. The stuck-forever case that motivated revisiting this was never
        # really routing — it was the chat banner failing open for a bound runner
        # missing from the fleet list, so the user was never offered the choice.
        # See frontend/src/components/chat/runnerEligibility.ts.
        q = q | (
            Q(chat_session__isnull=False)
            & (
                Q(chat_session__runner_binding__isnull=True)
                | Q(chat_session__runner_binding__runner__isnull=True)
                | Q(chat_session__runner_binding__runner=runner)
            )
        )
    own = None if ignore_unit else _agent_unit_slug(runner.unit())
    if own is not None:
        # An agent unit targets its one agent's turns and chats, nothing else —
        # no repo turns, no agentless chats (`turn_unit_refusal`). `ignore_unit`
        # is the unclaimable report's counterfactual only.
        q &= Q(agent__slug=own) | Q(chat_session__agent__slug=own)
    return q


def runner_tenant_slugs(runner: Runner) -> set[str]:
    """THE tenant a runner may act for — the workspaces of the human who PAIRED
    it, never the Runner.workspace FK.

    One definition, called by every runner-scoped predicate, because this rule
    diverging across call sites is a production outage and not a nicety: claim
    routing once scoped to the FK while `_runner_schedule_qs` derived from
    `owner`, so a runner could SEE and FIRE a schedule whose turn it could
    never CLAIM. One laptop runner deliberately serves a fleet spanning
    workspaces, so that stopped 4 of 5 production agents from executing at all
    and their turns sat QUEUED forever (2026-07-25).

    The FK records where a runner LIVES; `owner` records who it may work
    FOR. `owner` is server-assigned from `request.user` at pairing, so
    unlike the caller-supplied `capabilities` hint it is not attacker-
    controlled. A NULL `owner` fails closed (empty set → `__in=set()`
    matches nothing): an orphaned runner has no identity to derive a tenant
    from, and inferring one from the FK would be an escalation.

    And only the workspaces where that human may RUN work (`AGENT_WORK`,
    editor and above): a box claims other people's prompts, so a VIEWER's box
    serves nothing there — before 2026-10-02 a viewer could pair a session-
    capable box and receive the workspace's unbound chat sends.

    And only the workspaces the box is ENROLLED in, or that sit below one it is
    enrolled in (`RunnerEnrollment`, `covered_slugs`, 2026-10-11): the owner's memberships say where a box MAY work, enrollment
    says where it DOES. Without it a box served every workspace its owner could
    work in, so becoming owner of a separate tree (a family workspace) put every
    Dimagi box on that tree's repo turns and unbound chats, and back. The two
    INTERSECT — enrollment never grants more than membership, and a demoted or
    departed owner's box stops serving at once, enrolled or not.
    """
    if not runner.owner_id:
        return set()
    from apps.workspaces import permissions as perms

    covered = covered_slugs(runner)
    if not covered:
        return set()
    return perms.slugs_with(runner.owner, perms.AGENT_WORK, within=covered)


def covered_slugs(runner: Runner) -> set[str]:
    """The workspaces this runner's enrollments REACH: each enrolled workspace
    and every workspace below it in the tree (2026-10-11, owner decision). So a
    division created under `dimagi` after the box enrolled there is served with
    no re-enrolling — while a separate ROOT (`marshellis`) is never below
    anything the Dimagi boxes are enrolled in, and stays unserved. Coverage is
    still intersected with the owner's `agent.work` in `runner_tenant_slugs`,
    workspace by workspace: reaching a division does not make its owner an
    editor there."""
    enrolled = enrolled_slugs(runner)
    if not enrolled:
        return set()
    return enrolled | wsvc.descendant_slugs(enrolled)


def enrolled_slugs(runner: Runner) -> set[str]:
    """The workspaces this runner has opted in to serving (`RunnerEnrollment`),
    regardless of whether its owner may still work there — `runner_tenant_slugs`
    is the eligibility answer. Reads a `prefetch_related("enrollments")` when the
    caller made one, so a fleet-wide listing costs one query, not one per box."""
    cache = getattr(runner, "_prefetched_objects_cache", {})
    if "enrollments" in cache:
        return {e.workspace_id for e in cache["enrollments"]}
    from .models import RunnerEnrollment

    return set(RunnerEnrollment.objects.filter(runner_id=runner.pk)
               .values_list("workspace_id", flat=True))


def agent_tenant_q(ws_slugs, *, prefix: str = "agent") -> Q:
    """THE tenancy predicate for a row that derives its tenant from an Agent
    (a `Turn.agent`, an `AgentSchedule.agent`).

    There is NO null-workspace escape hatch, and there is nowhere left to put
    one: `Agent.workspace` is NOT NULL as of agents/0013. It used to read
    `Q(...__workspace_id__in=slugs) | Q(...__workspace_id__isnull=True)`, an
    ALLOW-on-NULL leg that made a workspace-less agent claimable and its
    schedules readable by every tenant. That leg existed in six predicates
    across the codebase and was fixed four times one site at a time (PRs #378,
    #421, #423) before the column itself was constrained.

    Callers must pass a slug set from `runner_tenant_slugs` (runner-scoped) or
    from the caller's own memberships (user-scoped) — this function deliberately
    does not compute it, so it can serve both.

    Note the `prefix` traversal is only ever valid on rows KNOWN to have an
    agent. `agent__workspace_id` traverses a nullable FK, so on a project or
    session turn (`agent_id IS NULL`) the LEFT JOIN yields NULL and any
    `isnull=True` leg would match unconditionally — the second, independent
    reason the old shape leaked. Every caller therefore ANDs this with
    `Q(agent__isnull=False)`.
    """
    return Q(**{f"{prefix}__workspace_id__in": ws_slugs})


#: The profile-enforcement version a runner must REPORT to be given a restricted
#: turn. 1 = the runner opens a caller's turn in its own `cx-` session with its
#: profile written first, AND the installed canopy guard confines that session.
#: 3 (2026-09-26) = runners now report the installed GUARD's version, and 3 is the
#: guard that checks writes against `write_paths`. A runner on older code reports
#: 1 or 2 whatever its guard, so it stops getting caller turns until it updates —
#: fail closed, rather than trusting a guard that lets a caller overwrite the script
#: its bash allowlist runs.
PROFILES_VERSION = 3
#: The caller-envelope version a runner's code must REPORT (`envelope`) to be
#: given a restricted turn. 2 (2026-10-04) = it reads `profile: "confined"` (the
#: envelope's VERSION 2 word for what was "restricted"). A runner on older code
#: tests `profile == "restricted"` and would run a confined turn in the FULL
#: profile, so it gets no caller turns until it updates — fail closed.
ENVELOPE_VERSION = 2


def profile_q(runner) -> Q:
    """Restricted turns only for a runner that can confine them.

    A caller's turn (`Turn.capability` set) claimed by a runner that cannot
    enforce the capability's profile would run in the agent's FULL profile —
    the exact outcome the declared interface exists to prevent, and silently.
    So a runner that has not reported `profiles` (an older laptop runner, the
    cloud runner today) never sees one. Deliberately NOT bypassable by a pin:
    a pin is a placement, never a way past a security property.
    """
    if (int(runner.capabilities.get("profiles") or 0) >= PROFILES_VERSION
            and int(runner.capabilities.get("envelope") or 0) >= ENVELOPE_VERSION):
        return Q()
    return Q(capability="")


def seed_assignments_from_capabilities(
    agent_model=None, runner_model=None, assignment_model=None
) -> int:
    """One-time bridge from the old two-sided routing config (runner
    capabilities.agents ∩ agent.runner_preference kind order) into explicit
    RunnerAssignment rows. Idempotent: skips (agent, runner) pairs that already
    have a row. Returns rows created. Used by the seed data migration.

    Takes the model classes so the migration can pass its HISTORICAL models and
    the test can pass nothing and get the live ones — same rule for both, which
    is the pattern `canopy_sessions.staleness.archive_stale_sessions` already
    follows.

    This is not stylistic. Reading live models from a data migration means the
    query names every column the model has TODAY against the schema as it stood
    at that migration, so the next field added to `Runner` breaks `migrate` from
    zero — every fresh dev DB and the whole CI suite, in a file nobody touched.
    Adding `code_version`/`code_sha` (spec 2026-07-28) is exactly what tripped it.
    """
    from apps.agents import models as agent_models
    from apps.harness import models as harness_models

    agent_cls = agent_model or agent_models.Agent
    runner_cls = runner_model or harness_models.Runner
    assignment_cls = assignment_model or harness_models.RunnerAssignment

    created = 0
    # "retired" by VALUE, not Runner.RETIRED: a historical model carries fields,
    # not the class constants the live model defines.
    runners = list(runner_cls.objects.exclude(status="retired"))
    for agent in agent_cls.objects.all():
        matched = [r for r in runners if agent.slug in (r.capabilities.get("agents") or [])]
        pref = agent.runner_preference or []

        def sort_key(r):
            kind_rank = pref.index(r.kind) if r.kind in pref else len(pref)
            return (kind_rank, r.name)

        existing = set(
            assignment_cls.objects.filter(agent=agent).values_list("runner_id", flat=True)
        )
        next_rank = assignment_cls.objects.filter(agent=agent).count()
        for r in sorted(matched, key=sort_key):
            if r.id in existing:
                continue
            assignment_cls.objects.create(agent=agent, runner=r, rank=next_rank)
            next_rank += 1
            created += 1
    return created
