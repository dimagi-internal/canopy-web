"""/api/agents/{slug}/… — which runners do an agent's work: the ordered default list,
per-source rules and per-person routes, and the gate every routing write passes."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.harness import clis, routing, routing_audit, turn_asker
from apps.harness import runners as harness_runners
from apps.harness.actors import actor_of, normalize_actor
from apps.harness.api import runner_read_q
from apps.harness.models import Runner, RunnerAssignment, Turn, agent_turns_q
from apps.harness.routing import (
    agents_with_own_order,
    cannot_serve,
    default_order_source,
    inherited_orders,
    prime_machine_units,
    runner_tenant_slugs_of,
    unit_refusal,
)
from apps.harness.schemas import RoutableSource

from . import services
from .access import Standings
from .gates import (
    agent_for_read,
    agent_for_work,
)
from .route_refusals import refuse_auto_unless_admin
from .schemas import (
    AgentActorRouteIn,
    AgentActorRouteOut,
    AgentActorRouteRunnerOut,
    AgentDefaultOrderOut,
    AgentPinnableRunnerOut,
    AgentRoutePreviewOut,
    AgentRoutePreviewRunnerOut,
    AgentRunnerOut,
    AgentRunnerRowIn,
    AgentRunnerRuleBodyIn,
    AgentRunnerRuleOut,
    AgentRunnerRulesIn,
    AgentRunnersIn,
)

router = Router(auth=session_auth, tags=["agents"])


def _refuse_runners_that_cannot_hold(agent, runners) -> None:
    """403 naming every runner whose owner is not one of the agent's admins.

    Pointing an agent's work at a box hands that box the agent's whole identity
    (`services.runner_may_hold_agent`), and these writes are open to the editor
    tier — so the box, not just the caller, has to be trusted with the agent.
    """
    refused = sorted({r.name for r in runners if not services.runner_may_hold_agent(r, agent)})
    if refused:
        raise HttpError(
            403,
            f"runner(s) {', '.join(refused)} cannot run {agent.slug}: a runner's owner "
            "must be the agent's owner, a workspace owner, or one of its admins",
        )


def _runners_for_routing(request: HttpRequest, agent, ids, already) -> dict:
    """The runners a routing write names, by id — gated on the ones it ADDS.

    THE gate for every write that points an agent's work at boxes (the default
    list, a source rule, a person's route). A runner being ADDED must live where
    it could serve this agent (the agent's workspace or one above it — the
    fleet's boxes live in `dimagi`, its agents in `connect`), be one the caller
    ADMINISTERS (owns, or holds a grant on), and be trusted with the agent. A
    runner the write merely KEEPS (`already`: on this list or rule now) is taken
    as it is.

    Why keeping is free: routing is a shared object. ACE's rules name the fleet's
    cloud boxes (jjackson's) and Sarvesh's laptop (his), and when every runner was
    re-checked against the caller, NO one could save ACE's rules at all — not even
    unchanged (2026-10-05, #1143). The gate exists so a box can't be attached by
    someone without rights over it; re-sending a row someone with those rights
    already wrote attaches nothing.

    A box the caller neither administers nor can otherwise see answers "unknown",
    never "forbidden": a guessed UUID must not learn that a runner exists.
    """

    ids = list(ids)
    already = set(already)
    added = [rid for rid in dict.fromkeys(ids) if rid not in already]
    # A computer's key holder (`Runner.COMPUTER`) takes no routed work: never a pick.
    live = Runner.objects.filter(id__in=ids).exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
    by_id = {r.id: r for r in live.filter(id__in=already)}

    home = [agent.workspace_id, *(agent.workspace.ancestor_slugs() if agent.workspace_id else [])]
    # ...or, with no workspace at all, the caller's own (a box with no tenant is
    # its owner's alone — the null leg `_runner_visibility_q` always allowed).
    candidates = list(live.filter(id__in=added).filter(
        Q(workspace_id__in=home) | Q(workspace_id__isnull=True, owner=request.user)))
    seen = set(Runner.objects.filter(id__in=[r.id for r in candidates])
               .filter(runner_read_q(request)).values_list("id", flat=True))
    refused = []
    for r in candidates:
        if harness_runners.can_administer_runner(request.user, r):
            by_id[r.id] = r
        elif r.id in seen:
            refused.append(r.name)
    missing = [str(rid) for rid in ids if rid not in by_id and rid not in
               {r.id for r in candidates if r.name in refused}]
    if missing:
        raise HttpError(422, f"unknown or retired runner id(s): {', '.join(missing)}")
    if refused:
        raise HttpError(
            403,
            f"you don't administer runner(s) {', '.join(sorted(refused))} — ask its owner to "
            "grant you admin (POST /api/harness/runners/{id}/admins)",
        )
    _refuse_runners_that_cannot_hold(agent, [by_id[rid] for rid in added])
    _refuse_other_units(agent, [by_id[rid] for rid in added])
    return by_id


def _refuse_other_units(agent, runners) -> None:
    """422 naming every runner kept from this agent by what it serves
    (`routing.unit_refusal`): an agent's runner runs only its own agent, and an agent
    with a runner of its own runs on no other runner of that machine. The claim
    refuses them anyway; refusing the write says so while someone is looking."""

    said = sorted({why for why in (unit_refusal(r, agent.slug) for r in runners) if why})
    if said:
        raise HttpError(422, f"cannot route {agent.slug} there: {'; '.join(said)}")


# ---- runner assignments (the routing-matrix UI's read/write surface) ----
@router.get("/{slug}/runners", response=list[AgentRunnerOut],
            summary="List the agent's ordered runner assignments")
@gate("member")
def list_agent_runners(request: HttpRequest, slug: str) -> list[AgentRunnerOut]:
    agent = agent_for_read(request, slug)
    return [
        AgentRunnerOut(
            runner_id=a.runner_id,
            runner_name=a.runner.name,
            kind=a.runner.kind,
            rank=a.rank,
            online=a.runner.live_status == a.runner.ONLINE,
            ready=a.runner.ready,
            enabled=a.enabled,
        )
        # source="" ONLY: this endpoint is the DEFAULT ordered list. A source
        # rule lives in the same table and would otherwise render as a phantom
        # chip in the order row.
        for a in agent.runner_assignments.filter(source="").select_related("runner")
    ]


@router.get("/{slug}/default-order", response=AgentDefaultOrderOut,
            summary="What the agent's work runs on when no rule matches")
@gate("member")
def get_agent_default_order(request: HttpRequest, slug: str) -> AgentDefaultOrderOut:
    """Its own ordered runner list when it has one; otherwise the default order of
    its workspace, or of the nearest workspace above it that has one, which the
    agent follows live. Saving an empty list to `PUT /runners` makes an agent
    follow its workspace again; its source rules are unaffected either way."""

    agent = agent_for_read(request, slug)
    if agent.pk in agents_with_own_order([agent.pk]):
        # `workspace` still names the order it WOULD follow, so a screen can
        # offer "follow it instead". `runners` is the agent's own list, the same
        # rows `GET /runners` serves: leaving it empty here read as "routes
        # nowhere" to every caller of this endpoint (#1233).
        return AgentDefaultOrderOut(own=True, workspace=default_order_source(agent),
                                    runners=list_agent_runners(request, slug),
                                    repo_url=agent.repo_url or "")
    inh = inherited_orders([agent.pk]).get(agent.pk)
    if inh is None:
        return AgentDefaultOrderOut(own=False, repo_url=agent.repo_url or "")
    return AgentDefaultOrderOut(
        own=False,
        workspace=inh.workspace,
        runners=[
            AgentRunnerOut(runner_id=r.pk, runner_name=r.name, kind=r.kind, rank=i,
                           online=r.live_status == r.ONLINE, ready=r.ready)
            for i, r in enumerate(inh.runners)
        ],
        missing_repo=[r.name for r in inh.missing_repo],
        missing_repo_ids=[r.pk for r in inh.missing_repo],
        cannot_hold=[r.name for r in inh.cannot_hold],
        directed=[r.name for r in inh.directed],
        repo_url=agent.repo_url or "",
    )


@router.get("/{slug}/pinnable-runners", response=list[AgentPinnableRunnerOut],
            summary="The runners you may start a chat with the agent on")
@gate("member")
def list_agent_pinnable_runners(request: HttpRequest, slug: str) -> list[AgentPinnableRunnerOut]:
    """Every box you may choose for a new chat with this agent, in or out of its
    runner order. Choosing one pins the chat to it, past the order and any strict
    source rule; the agent's admins may choose any box that can run it, anyone
    else only a box they administer. Boxes in the order come first, by rank."""

    # The same gates a placement passes (canopy_sessions.placement._placeable_runner
    # and may_place), read once for the whole fleet rather than per box.
    agent = agent_for_read(request, slug)
    who = turn_asker.subject(request.user)
    runners = [
        r for r in Runner.objects.filter(owner__isnull=False)
        .exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
        .select_related("owner").prefetch_related("enrollments")
        if r.session_capable()
    ]
    tenants = runner_tenant_slugs_of(runners)
    runners = [r for r in runners if agent.workspace_id in tenants[r.pk]]
    if not agent.is_admin(who):
        mine = harness_runners.administered_runner_ids(who, runners)
        runners = [r for r in runners if r.pk in mine]
    standings = Standings({r.owner for r in runners}, [agent])
    prime_machine_units(runners)
    offered = set(clis.offered(agent))
    runners = [
        r for r in runners
        if services.runner_may_hold_agent(r, agent, standings=standings)
        and not cannot_serve(r, agent.slug)
        and offered & set(clis.reported(r))
    ]
    order = (agent.runner_assignments.filter(source="", enabled=True).values_list("runner_id", flat=True)
             if agent.pk in agents_with_own_order([agent.pk])
             else [r.pk for r in getattr(inherited_orders([agent.pk]).get(agent.pk), "runners", [])])
    rank = {rid: i for i, rid in enumerate(order)}
    runners.sort(key=lambda r: (rank.get(r.pk, len(rank)), r.live_status != r.ONLINE, r.name))
    return [
        AgentPinnableRunnerOut(runner_id=r.pk, runner_name=r.name, kind=r.kind,
                               online=r.live_status == r.ONLINE, ready=r.ready, rank=rank.get(r.pk))
        for r in runners
    ]
@router.get("/{slug}/route-preview", response=AgentRoutePreviewOut,
            summary="Where the agent's work would go right now")
@gate("member")
def get_agent_route_preview(
    request: HttpRequest, slug: str, source: RoutableSource = Turn.ORIGIN_CANOPY_WEB_CHAT,
    actor: str = "",
) -> AgentRoutePreviewOut:
    """The runners a turn from `actor` (default: you) on `source` (default: a
    canopy-web chat) would be offered, in order, with the rule that put each
    there, and the one that would take it now. What "Auto" means for that work."""

    # The claim's own ladder (routing.labelled_assignment_rows over
    # load_assignment_rows) with no pin, binding or turn requirements: a new turn.
    agent = agent_for_read(request, slug)
    me = normalize_actor(getattr(turn_asker.subject(request.user), "email", ""))
    who = normalize_actor(actor) if actor else me
    if actor and not who:
        raise HttpError(422, f"not an email address: {actor!r}")
    defaults, priorities = routing.load_assignment_rows([agent.pk])
    rows = routing.labelled_assignment_rows(agent.pk, source, who, defaults, priorities, serves=agent.slug)
    ladder = [priorities.get((agent.pk, source, who)) if who else None, priorities.get((agent.pk, source, ""))]
    strict = next((rule[0].strict for rule in ladder if rule), False)
    follows = (None if agent.pk in agents_with_own_order([agent.pk])
               else getattr(inherited_orders([agent.pk]).get(agent.pk), "workspace", None))
    runners = [
        AgentRoutePreviewRunnerOut(runner_id=r.pk, runner_name=r.name, kind=r.kind, rung=rung,
                                   available=r.is_available, status=_runner_state(r))
        for r, rung in rows
    ]
    picks = next((r for r in runners if r.available), None)
    return AgentRoutePreviewOut(
        source=source, actor=who, runners=runners, picks=picks.runner_id if picks else None,
        strict=strict, follows_workspace=follows,
        summary=_route_summary(agent, runners, picks, strict, follows,
                               person="your route" if who == me else f"the route for {who}"),
    )


def _runner_state(runner) -> str:
    live = runner.live_status
    if live != Runner.ONLINE:
        return "paused" if live == Runner.PAUSED else "offline"
    return "online" if runner.ready else "not ready"


def _route_summary(agent, runners, picks, strict, follows, *, person: str) -> str:
    default = f"the {follows} workspace order" if follows else f"{agent.name}'s runner order"
    if not runners:
        return ("Nothing can take it: the strict rule names no runner that can run "
                f"{agent.name}" if strict else f"Nothing can take it: {agent.name} has no runner that can run it")
    if picks is None:
        return f"Waits: no runner is available ({', '.join(f'{r.runner_name} is {r.status}' for r in runners)})"
    why = {routing.RUNG_PERSON: person, routing.RUNG_SOURCE: "the rule for this source"}.get(picks.rung, default)
    skipped = runners[:runners.index(picks)]
    tail = f" ({', '.join(f'{r.runner_name} is {r.status}' for r in skipped)})" if skipped else ""
    return f"Goes to {picks.runner_name}, from {why}{tail}"


@router.put("/{slug}/runners", response=list[AgentRunnerOut],
            summary="Replace the agent's ordered runner list (index = rank)")
@gate("agent.work", "runner-admin", setting=True)  # every ADDED runner administered + holdable
def replace_agent_runners(request: HttpRequest, slug: str, payload: AgentRunnersIn) -> list[AgentRunnerOut]:
    """Replace the agent's ORDERED runner list (index = rank) — the single
    routing authority (spec 2026-07-24). Wholesale replace: the matrix UI saves
    a full row, so there is no partial-update ambiguity.

    Accepts either form (exactly one must be provided — 422 otherwise):
    `runners` (ordered rows, each carrying its own `enabled` — a disabled row
    stays in the list, rank preserved, but never routes) or the legacy
    `runner_ids` (ordered ids, all implicitly enabled)."""

    agent = agent_for_work(request, slug)

    if (payload.runner_ids is None) == (payload.runners is None):
        raise HttpError(422, "provide exactly one of runner_ids or runners")

    if payload.runners is not None:
        rows_in: list[AgentRunnerRowIn] = payload.runners
    else:
        rows_in = [AgentRunnerRowIn(runner_id=rid, enabled=True) for rid in payload.runner_ids]

    ids = [row.runner_id for row in rows_in]
    # Reject duplicate runner IDs early
    if len(ids) != len(set(ids)):
        raise HttpError(422, "duplicate runner id in list")
    # Runners this list ADDS must be ones the caller administers; the ones it
    # keeps are taken as they are (`_runners_for_routing`).
    by_id = _runners_for_routing(
        request, agent, ids,
        RunnerAssignment.objects.filter(agent=agent, source="").values_list("runner_id", flat=True),
    )

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source="")
        # source="" ONLY. Source rules live in this table too, and an unscoped
        # delete here would destroy every one of them each time the default
        # order was saved.
        RunnerAssignment.objects.filter(agent=agent, source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(agent=agent, runner=by_id[row.runner_id], rank=i, enabled=row.enabled)
            for i, row in enumerate(rows_in)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope="default runner order",
            before=before, after=routing_audit.agent_rows(agent, source=""))
    return list_agent_runners(request, slug)


# ---- per-source routing rules (the exceptions to the ordered list above) ----
@router.get("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="List the agent's per-source routing rules")
@gate("member")
def list_agent_runner_rules(request: HttpRequest, slug: str) -> list[AgentRunnerRuleOut]:
    """The per-source overrides on top of the default ordered list (spec
    2026-07-27). One rule per source, max — the priority runner, and whether it is
    the only one allowed to take that source's work."""
    from collections import Counter

    agent = agent_for_read(request, slug)
    # Queued depth per (source, actor) — what the UI's "N turns are parked" warning
    # renders next to a strict rule whose runner is offline. This cannot stay a
    # `values_list("origin").annotate(Count)` now that the key includes the actor:
    # the actor is DERIVED per turn (from `origin_ref["from"]` for mail, from
    # `enqueued_by` otherwise), so it is grouped in Python. Queued sets are single
    # digits in practice, and a count keyed only on origin would attribute other
    # people's parked work to your rule.
    queued: Counter = Counter()
    for t in (
        Turn.objects.filter(agent_turns_q(agent), status=Turn.QUEUED)
        .select_related("enqueued_by", "initiator_contact")
        .only("origin", "origin_ref", "enqueued_by", "initiator_contact")
    ):
        queued[(t.origin, actor_of(t))] += 1

    rows = (
        RunnerAssignment.objects.filter(agent=agent).exclude(source="")
        .select_related("runner").order_by("source", "actor", "rank")
    )
    return [
        AgentRunnerRuleOut(
            source=row.source,
            actor=row.actor,
            rank=row.rank,
            runner_id=row.runner.id,
            runner_name=row.runner.name,
            kind=row.runner.kind,
            strict=row.strict,
            # Derived, like AgentRunnerOut: the stored status lies once a runner
            # goes quiet (heartbeat writes ONLINE and nothing demotes it).
            online=row.runner.live_status == Runner.ONLINE,
            ready=row.runner.ready,
            enabled=row.enabled,
            # Every row of a rule repeats its RULE's count: the parked queue belongs
            # to the rule, not to one runner in it.
            queued_count=queued.get((row.source, row.actor), 0),
            turn_mode=row.turn_mode,
        )
        for row in rows
    ]


def _current_rules(agent) -> dict:
    """(source, actor) -> {"ids": runner ids in the rule, "turn_mode": its mode}."""

    out: dict = {}
    for source, actor, runner_id, turn_mode in (
        RunnerAssignment.objects.filter(agent=agent).exclude(source="")
        .values_list("source", "actor", "runner_id", "turn_mode")
    ):
        rule = out.setdefault((source, actor), {"ids": set(), "turn_mode": turn_mode})
        rule["ids"].add(runner_id)
    return out


def _refuse_new_auto(request: HttpRequest, agent, source: str, actor: str, turn_mode: str,
                     current: dict) -> None:
    """Setting a rule to `auto` is for the agent's admins — but only SETTING it:
    keeping an `auto` an admin already set is not, or an editor could never save
    the rules around it."""
    if turn_mode == "auto" and current.get((source, actor), {}).get("turn_mode") != "auto":
        refuse_auto_unless_admin(request, agent, f"a routing rule ({source}/{actor or 'anyone'})")


@router.put("/{slug}/runner-rules", response=list[AgentRunnerRuleOut],
            summary="Replace the agent's per-source routing rules")
@gate("agent.work", "runner-admin", setting=True)  # every ADDED runner administered
def replace_agent_runner_rules(
    request: HttpRequest, slug: str, payload: AgentRunnerRulesIn
) -> list[AgentRunnerRuleOut]:
    """Wholesale replace, scoped to non-empty-source rows — the default ordered
    list belongs to PUT /runners and is left alone.

    A separate endpoint rather than a combined body so neither write can clobber
    the other's rows (they share one table), and so the existing GET response
    shape stays what the frontend already consumes.
    """

    agent = agent_for_work(request, slug)

    # Normalize BEFORE validating or storing, so a rule pasted straight out of a
    # mail client ("Sarvesh Tewari <STewari@Dimagi.com>") matches a turn whose
    # sender resolves to the bare lowercase address. Rejecting an unparseable
    # actor is the point: stored as-is it would be a rule that silently never
    # matches, which is indistinguishable from a routing bug.
    actors: list[str] = []
    for r in payload.rules:
        actor = normalize_actor(r.actor) if r.actor else ""
        if r.actor and not actor:
            raise HttpError(422, f"not an email address: {r.actor!r}")
        actors.append(actor)

    current = _current_rules(agent)

    # `auto` on a rule is the agent's admins' to set (docs/architecture/access.md).
    # Asked before the runners are, so an editor is told the actual reason.
    for r, actor in zip(payload.rules, actors):
        _refuse_new_auto(request, agent, r.source, actor, r.turn_mode, current)

    # One rule per (source, actor) — several actors MAY share a source, which is
    # the whole feature. Caught here rather than left to the DB constraint so the
    # caller gets a named reason instead of an IntegrityError 500.
    keys = list(zip([r.source for r in payload.rules], actors))
    if len(keys) != len(set(keys)):
        raise HttpError(422, "one rule per (source, actor): duplicate in list")

    for r, actor in zip(payload.rules, actors):
        if not r.runners:
            # A zero-length strict rule composes to an empty list and parks the
            # queue naming no runner as the reason. Deleting the rule is how you
            # turn it off.
            raise HttpError(422, f"a rule needs at least one runner: {r.source}/{actor}")
        seen = [row.runner_id for row in r.runners]
        if len(seen) != len(set(seen)):
            raise HttpError(422, f"a runner may appear once per rule: {r.source}/{actor}")

    # Gated per rule on the runners each one ADDS (`_runners_for_routing`): a
    # runner already in that rule is kept as it is, so re-sending someone else's
    # rule unchanged is not a claim to administer their box.
    by_id: dict = {}
    for r, actor in zip(payload.rules, actors):
        by_id.update(_runners_for_routing(
            request, agent, [row.runner_id for row in r.runners],
            current.get((r.source, actor), {}).get("ids", ()),
        ))

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, rules_only=True)
        RunnerAssignment.objects.filter(agent=agent).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            # `rank` is the runner's position WITHIN its rule — the list order the
            # caller sent. `strict` is rule-level and written to every row of the
            # rule, so the composer can read it off the first.
            RunnerAssignment(
                agent=agent, runner=by_id[row.runner_id], rank=rank,
                source=r.source, actor=actor, strict=r.strict, enabled=row.enabled,
                turn_mode=r.turn_mode,
            )
            for r, actor in zip(payload.rules, actors)
            for rank, row in enumerate(r.runners)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope="routing rules (all)",
            before=before, after=routing_audit.agent_rows(agent, rules_only=True))
    return list_agent_runner_rules(request, slug)


# ---- one rule at a time (#1143) ----
#
# A rule belongs to one (source, person) — the person's work, routed to boxes that
# may belong to someone else entirely. Saving ALL of an agent's rules to change
# one made every save depend on rights over every box any rule names, which no
# one holds once two people's boxes are involved. The Settings page saves here,
# one changed rule at a time; the wholesale PUT above stays for scripts.

def _rule_key_or_422(source: str, actor: str) -> tuple[str, str]:
    from typing import get_args

    if source not in get_args(RoutableSource):
        raise HttpError(422, f"not a routable source: {source!r}")
    if not actor:
        return source, ""
    actor = _actor_or_422(actor)
    if source == Turn.ORIGIN_CANOPY_SCHEDULER:
        raise HttpError(422, "no person is behind canopy_scheduler turns, so a person's rule can't apply there")
    return source, actor


@router.put("/{slug}/runner-rules/{source}", response=list[AgentRunnerRuleOut],
            summary="Set one routing rule (a source, optionally one person's)")
@gate("agent.work", "runner-admin", setting=True)  # every ADDED runner administered by caller
def set_agent_runner_rule(
    request: HttpRequest, slug: str, source: str, payload: AgentRunnerRuleBodyIn, actor: str = "",
) -> list[AgentRunnerRuleOut]:
    """Route `source` turns — only `actor`'s, when given — to these runners, in
    preference order. Replaces that ONE rule and touches no other. Runners the
    rule already names are kept as they are; a runner it ADDS must be one you
    administer. `strict`: only these runners may take the work. Returns every rule."""

    agent = agent_for_work(request, slug)
    source, actor = _rule_key_or_422(source, actor)
    if not payload.runners:
        raise HttpError(422, "a rule needs at least one runner — DELETE removes a rule")
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "a runner may appear once per rule")
    current = _current_rules(agent)
    _refuse_new_auto(request, agent, source, actor, payload.turn_mode, current)
    by_id = _runners_for_routing(request, agent, ids, current.get((source, actor), {}).get("ids", ()))

    scope = f"{source} rule" + (f" for {actor}" if actor else "")
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source=source, actor=actor)
        RunnerAssignment.objects.filter(agent=agent, source=source, actor=actor).delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(
                agent=agent, runner=by_id[row.runner_id], rank=rank, source=source,
                actor=actor, strict=payload.strict, enabled=row.enabled,
                turn_mode=payload.turn_mode,
            )
            for rank, row in enumerate(payload.runners)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=scope, before=before,
            after=routing_audit.agent_rows(agent, source=source, actor=actor))
    return list_agent_runner_rules(request, slug)


@router.delete("/{slug}/runner-rules/{source}", response={204: None},
               summary="Remove one routing rule")
@gate("agent.work", setting=True)
def delete_agent_runner_rule(request: HttpRequest, slug: str, source: str, actor: str = ""):
    """Remove the rule for `source` (and `actor`, when given). That work goes back
    to the next rule down and the agent's default order. Idempotent."""

    agent = agent_for_work(request, slug)
    source, actor = _rule_key_or_422(source, actor)
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, source=source, actor=actor)
        RunnerAssignment.objects.filter(agent=agent, source=source, actor=actor).delete()
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"{source} rule" + (f" for {actor}" if actor else ""),
            before=before, after=[])
    return Status(204, None)


# ---- per-person routing (the additive face of the actor rules above) ----
#
# The same RunnerAssignment rows as /runner-rules, addressed by PERSON instead of
# wholesale. Two things made "route Alice to my laptop" impractical there:
#   1. a person spans five sources, so it took five hand-built rules; and
#   2. the PUT replaces every rule and requires the caller to have paired every
#      runner it names — so anyone but the operator who owns ALL the boxes got a
#      422 just for re-sending the rules already there.
# These routes touch one actor's rows only, and gate on the runners being NAMED.

def _actor_or_422(actor: str) -> str:

    normalized = normalize_actor(actor)
    if not normalized:
        raise HttpError(422, f"not an email address: {actor!r}")
    return normalized


def _actor_sources() -> list[str]:
    """Every routable source whose turns carry a person. The scheduler is the
    one that doesn't (`apps/harness/actors.py`), so an actor rule on it is dead."""
    from typing import get_args

    return [s for s in get_args(RoutableSource) if s != Turn.ORIGIN_CANOPY_SCHEDULER]


@router.get("/{slug}/actor-routes", response=list[AgentActorRouteOut],
            summary="List which people's work routes to which runners")
@gate("member")
def list_agent_actor_routes(request: HttpRequest, slug: str) -> list[AgentActorRouteOut]:
    """Per-person routing for this agent: for each person with a rule, the runners
    their work goes to (in preference order), whether that is strict, and on which
    sources. People with no entry follow the agent's source rules and default
    runner order."""
    rules = list_agent_runner_rules(request, slug)
    # (actor, source) -> rows of that rule, rank order (the list is pre-sorted).
    by_rule: dict[tuple[str, str], list[AgentRunnerRuleOut]] = {}
    for row in rules:
        if row.actor:
            by_rule.setdefault((row.actor, row.source), []).append(row)

    grouped: dict[tuple, AgentActorRouteOut] = {}
    for (actor, source), rows in by_rule.items():
        key = (actor, tuple((r.runner_id, r.enabled) for r in rows), rows[0].strict, rows[0].turn_mode)
        route = grouped.get(key)
        if route is None:
            route = grouped[key] = AgentActorRouteOut(
                actor=actor,
                runners=[
                    AgentActorRouteRunnerOut(
                        runner_id=r.runner_id, runner_name=r.runner_name,
                        online=r.online, enabled=r.enabled,
                    )
                    for r in rows
                ],
                strict=rows[0].strict, sources=[], turn_mode=rows[0].turn_mode,
            )
        route.sources.append(source)
        route.queued_count += rows[0].queued_count
    return sorted(grouped.values(), key=lambda r: (r.actor, r.sources))


@router.put("/{slug}/actor-routes/{actor}", response=list[AgentActorRouteOut],
            summary="Route one person's work to specific runners")
@gate("agent.work", "runner-admin", setting=True)  # every ADDED runner administered by caller
def set_agent_actor_route(
    request: HttpRequest, slug: str, actor: str, payload: AgentActorRouteIn
) -> list[AgentActorRouteOut]:
    """Send everything this person asks this agent to do (by email, Slack, chat,
    ace-web or API) to the given runners, in preference order. Replaces only THIS
    person's routing; everyone else's rules are untouched.

    `actor` is the person's email. With `strict` (the default) only the named
    runners may take their work — if those are offline it waits; with
    `strict=false` it falls back to the agent's usual runners after a minute.
    You may name only runners you administer (you own it, or its owner
    granted you admin) — this decides where work runs, so it is gated on the box,
    not just the agent. Remove a route with DELETE on the same path.
    """

    agent = agent_for_work(request, slug)
    actor = _actor_or_422(actor)
    current = _current_rules(agent)

    sources = list(dict.fromkeys(payload.sources or _actor_sources()))
    dead = [s for s in sources if s not in _actor_sources()]
    if dead:
        raise HttpError(422, f"no person is behind {', '.join(dead)} turns, so a person's route can't apply there")
    if not payload.runners:
        raise HttpError(422, "a route needs at least one runner — DELETE removes a route")
    ids = [row.runner_id for row in payload.runners]
    if len(ids) != len(set(ids)):
        raise HttpError(422, "a runner may appear once per route")

    # Per source, like the rules it writes: `auto` and the runners are gated on
    # what changes in each (`_refuse_new_auto`, `_runners_for_routing`).
    runners: dict = {}
    for source in sources:
        _refuse_new_auto(request, agent, source, actor, payload.turn_mode, current)
        runners.update(_runners_for_routing(
            request, agent, ids, current.get((source, actor), {}).get("ids", ())))

    with transaction.atomic():
        before = routing_audit.agent_rows(agent, actor=actor, rules_only=True)
        RunnerAssignment.objects.filter(agent=agent, actor=actor).exclude(source="").delete()
        RunnerAssignment.objects.bulk_create([
            RunnerAssignment(
                agent=agent, runner=runners[row.runner_id], rank=rank, source=source,
                actor=actor, strict=payload.strict, enabled=row.enabled,
                turn_mode=payload.turn_mode,
            )
            for source in sources
            for rank, row in enumerate(payload.runners)
        ])
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"route for {actor}", before=before,
            after=routing_audit.agent_rows(agent, actor=actor, rules_only=True))
    return [r for r in list_agent_actor_routes(request, slug) if r.actor == actor]


@router.delete("/{slug}/actor-routes/{actor}", response={204: None},
               summary="Remove one person's routing")
@gate("agent.work", setting=True)
def delete_agent_actor_route(request: HttpRequest, slug: str, actor: str):
    """Stop routing this person's work specially: their turns go back to the
    agent's source rules and default runner order. Touches no one else's rules.
    Idempotent — removing a person with no route is a 204."""

    agent = agent_for_work(request, slug)
    actor = _actor_or_422(actor)
    with transaction.atomic():
        before = routing_audit.agent_rows(agent, actor=actor, rules_only=True)
        RunnerAssignment.objects.filter(agent=agent, actor=actor).exclude(source="").delete()
        routing_audit.record_routing_change(
            request, workspace=agent.workspace,
            target=agent.slug, scope=f"route for {actor}", before=before, after=[])
    return Status(204, None)
