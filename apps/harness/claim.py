"""The claim: a runner asks for work and gets at most one turn.

`claim_next_turn` is a single conditional UPDATE, so no row can be claimed twice,
and expired leases are swept lazily on the same call (`sweep_expired_leases`).
Its two explanations live beside it because they must apply the same rules:
`turn_reach` (will a live runner pick this up? — what the turn status and Slack
say) and `unclaimable_queued_turns` (the web's stuck-turn warning).

Not `claiming.py`, which builds the payload a claimed turn is SENT as. Split out
of `services.py`, which still re-exports every name here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.harness import actors
from apps.harness import runner_requirements as rr
from apps.workspaces import services as wsvc

from .ledger import append_events
from .models import (
    FleetHold,
    Runner,
    RunnerAssignment,
    Turn,
    WorkspaceRunnerOrder,
)
from .routing import (
    MIDTURN_FAILED,
    _assignment_allows,
    _assignment_allows_for_agent,
    _is_repo_turn,
    _kind_allows,
    _repo_order_allows,
    agent_tenant_q,
    assignment_rows_for,
    blocking_turn,
    busy_agent_ids,
    busy_session_ids,
    cannot_serve,
    is_project_session_turn,
    load_assignment_rows,
    load_workspace_orders,
    prime_machine_units,
    profile_q,
    project_session_allows,
    project_session_covers,
    project_session_waits_why,
    repo_order_for,
    ridable_sessions,
    routed_slug,
    runner_target_q,
    runner_tenant_slugs,
    turn_unit_refusal,
)
from .closing import close_turn
from .drills import expire_stale_drills
from .turn_agent_tokens import revoke_stale
from .schedule_turns import release_stale_occurrence_turns_all, skip_late_scheduled_turns

DEFAULT_LEASE_SECONDS = 900


def sweep_expired_leases() -> int:
    now = timezone.now()
    expired = list(
        Turn.objects.filter(
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
            lease_expires_at__lt=now,
        )
    )
    count = 0
    for turn in expired:
        # A turn with cancel_requested already in its ledger closes CANCELLED
        # instead of LOST — the runner never got the chance to act on the
        # cancel signal before its lease expired, but the intent was still to
        # stop, not to lose the turn.
        requested = turn.events.filter(kind="cancel_requested").exists()
        status = Turn.CANCELLED if requested else Turn.LOST
        summary = (
            "drill turn cancelled (lease expired after cancel was requested)"
            if status == Turn.CANCELLED
            else "runner lost the turn (lease expired mid-drill)"
        )
        # The result note is left as it was: nothing reported one. Riders are
        # not ended with it — each holds its own lease and is swept on its own.
        if close_turn(turn, status, from_states=Turn.NON_TERMINAL,
                      where={"lease_expires_at__lt": now}, now=now,
                      event={"reason": "lease_expired"}, riders=False,
                      drill_summary=summary):
            count += 1
    # The catch-all: a turn requeued or ended by any other queryset update.
    revoke_stale()
    return count


# A turn is not "stuck" the instant it is enqueued — it is queued for a few seconds
# on every normal send while a runner polls (5s) or the WS wake fires. And a runner
# whose heartbeat lapses (>90s) reads STALE, so a flaky laptop network briefly looks
# like "no runners at all". Both made the warning fire on healthy traffic: a phone
# chat send was flagged during a DNS blip, then claimed and answered seconds later.
# Wait longer than a heartbeat window + a claim poll before calling anything stuck.
UNCLAIMABLE_GRACE = dt.timedelta(seconds=150)


def _assignment_rows_for_turns(turns) -> tuple[dict, dict]:
    """The SAME rows claim_next_turn composes from, so the two answers cannot
    diverge — including the session leg's agent, which routes by its agent's
    rules while the session is not yet bound."""
    agent_ids = {t.agent_id for t in turns if t.agent_id} | {
        t.chat_session.agent_id
        for t in turns
        if t.chat_session_id and t.chat_session.agent_id
    }
    return load_assignment_rows(agent_ids)


def _may_hold(r: Runner, t: Turn, holds: dict | None) -> bool:
    """claim_next_turn's `runner_may_hold_agent` gate, cached per (runner, agent)."""
    agent = t.acting_agent
    if agent is None:
        return True
    from apps.agents.services import runner_may_hold_agent

    if holds is None:
        return runner_may_hold_agent(r, agent)
    key = (r.id, agent.id)
    if key not in holds:
        holds[key] = runner_may_hold_agent(r, agent)
    return holds[key]


def _refined_allows(r: Runner, t: Turn, defaults: dict, priorities: dict,
                    *, ignore_requirements: bool = False, orders: dict | None = None,
                    ignore_hold: bool = False, holds: dict | None = None,
                    ignore_serve: bool = False, admins: dict | None = None) -> bool:
    """The per-candidate refinements claim_next_turn applies after the coarse
    target match — same checks, same ORDER, so coverage can't overstate what
    claiming will do.

    `ignore_requirements` answers the counterfactual "would this runner take it
    if the conversation required nothing?" — only ever asked to decide whether
    a requirement is what BLOCKS a turn, never to route one. `ignore_hold` is
    the same counterfactual for `runner_may_hold_agent`, and `ignore_serve` for a
    box's own report that it cannot run the agent (`routing.cannot_serve`)."""
    reqs = frozenset() if ignore_requirements else rr.requirements_of(t)
    if not rr.satisfies(r.flags, reqs):
        return False  # above the pin and the binding, as in claim_next_turn
    if not ignore_hold and not _may_hold(r, t, holds):
        return False  # likewise above the pin and the binding
    if not ignore_serve and turn_unit_refusal(r, t):
        return False  # the box's isolation unit: above the pin and the binding too
    # A pin trumps everything below it (claim_next_turn's `pinned_here`).
    if t.pinned_runner_id == r.id:
        return True
    directed = r.directed_only()
    if t.chat_session_id:
        # STICKINESS, mirroring the claim loop's bound_to_me short-circuit: a
        # session bound to this runner claims on it regardless of assignments.
        # Surviving runner_target_q is what IDENTIFIES the holder, so running
        # the assignment check here would report a live chat as `config`
        # ("no runner is assigned") while claiming takes it happily.
        binding = getattr(t.chat_session, "runner_binding", None)
        if binding is not None and binding.runner_id == r.id:
            return True
        if binding is not None and binding.runner_id is not None:
            return False  # bound to another box: only its holder claims it
        routed_agent = t.chat_session.agent_id
    else:
        routed_agent = t.agent_id
    if directed:
        return False  # a directed-only box takes pins and its bindings, nothing routed
    if not routed_agent:
        if _is_repo_turn(t):
            # A repo turn follows its workspace's runner order when it has one:
            # membership only, as for an agent's list (the cascade is about WHEN,
            # not WHETHER — a lower rank still covers the turn).
            rows = repo_order_for(t, orders if orders is not None
                                  else load_workspace_orders({t.workspace_id}), requires=reqs)
            return rows is None or any(row.id == r.id for row in rows)
        # An unbound project chat: the box has its repo, and the workspace's
        # order shares the box or the chatting person administers it — the
        # predicate the claim asks, without the cascade's clock.
        return project_session_covers(
            r, t, orders if orders is not None else _orders_for([t]), requires=reqs,
            admins=admins)
    # The SAME actor resolution the claim path uses. These two disagreeing is
    # the drift class tests/test_claim_schedule_parity.py exists for: a strict
    # actor rule pointing at an offline box must report `offline`
    # (recoverable), never `config` (never runs).
    rows = assignment_rows_for(
        routed_agent, t.origin, actors.actor_of(t), defaults, priorities, requires=reqs,
        serves="" if ignore_serve else routed_slug(t, routed_agent),
    )
    return any(row_runner.id == r.id for _rank, row_runner in rows)


def _coverage(ids, runners, defaults: dict, priorities: dict,
              *, ignore_requirements: bool = False, ignore_hold: bool = False,
              ignore_serve: bool = False) -> dict:
    """{runner: {turn pk it could claim}} over `ids` — the coverage half of both
    `unclaimable_queued_turns` and `turn_reach`, one implementation so the web
    warning and the Slack acknowledgement cannot disagree about the same turn.

    `ignore_requirements=True` is the counterfactual used only to diagnose: which
    runners would take the turn if its conversation required nothing;
    `ignore_hold=True` likewise, if every runner's owner could hold the agent;
    `ignore_serve=True`, if no box had reported it cannot run the agent."""
    out: dict = {}
    holds: dict = {}
    admins: dict = {}
    orders = load_workspace_orders(
        set(Turn.objects.filter(pk__in=ids, agent__isnull=True, chat_session__isnull=True)
            .values_list("workspace_id", flat=True))
        | set(Turn.objects.filter(pk__in=ids, chat_session__isnull=False,
                                  chat_session__agent__isnull=True)
              .values_list("chat_session__workspace_id", flat=True))
    )
    for r in runners:
        # Same coarse target predicate the claim path uses (assignments +
        # projects + binding-sticky sessions), plus the pin arm — a turn
        # pinned to an offline standby must read "offline", not "config".
        q = runner_target_q(r, ignore_unit=ignore_serve) | Q(pinned_runner=r)
        covered = set()
        for t in (
            Turn.objects.filter(pk__in=ids).filter(q)
            # A turn pinned ELSEWHERE is invisible to this runner in the claim
            # path; the coarse predicate above does not say so on its own.
            .filter(Q(pinned_runner__isnull=True) | Q(pinned_runner=r))
            .filter(profile_q(r))
            .select_related("agent", "chat_session", "chat_session__runner_binding",
                            "chat_session__agent", "enqueued_by")
        ):
            # Then the per-source refinement. A runner assigned the agent but
            # excluded by a strict rule for THIS turn's source does not cover
            # it, and saying otherwise would mask a genuinely parked queue.
            if _refined_allows(r, t, defaults, priorities,
                               ignore_requirements=ignore_requirements, orders=orders,
                               ignore_hold=ignore_hold, holds=holds,
                               ignore_serve=ignore_serve, admins=admins):
                covered.add(t.pk)
        out[r] = covered
    return out


LIVE, OFFLINE, PARKED, UNROUTED = "live", "offline", "parked", "config"
#: Its runners are online, but every one that could take it says its Claude login
#: is gone (`Runner.signed_out`) — so none will claim it until someone signs in.
SIGNED_OUT = "signed_out"


@dataclass
class Reach:
    """Where one queued turn stands, right now.

    `live` — an ONLINE runner will claim it; `runners` are those runners.
    `offline` — runners could take it, none is online; `runners` are those.
    `parked` — every runner that could take it is PAUSED (someone decided, not a
    lapsed heartbeat); `runners` are those. `held_until` says when the first of
    them resumes on its own, or None when none will without a person.
    `signed_out` — runners are online, but every one that could take it has no
    Claude sign-in (`Runner.signed_out`); `runners` are those.
    `config` (UNROUTED) — nothing could ever take it until routing changes.
    """

    kind: str
    runners: list
    #: True only when the conversation's runner requirements (ZDR) are what
    #: keeps it from a live runner: some runner would take it without them. A
    #: turn stuck for another reason (no routing, a box that cannot confine a
    #: caller's turn) must not be blamed on ZDR — that sends its owner to the
    #: wrong fix.
    blocked_by_requirements: bool = False
    #: The executing turn this one waits behind (`blocking_turn`): its session's
    #: previous message, or its agent's current turn. A LIVE runner will take it,
    #: but not until that turn ends — "picking this up" would be a promise the
    #: claim is not keeping (canopy-web#1147).
    behind: Turn | None = None
    #: For PARKED: when the first of its runners resumes on its own (a scheduled
    #: unpause), or None when only a person resuming one will run it.
    held_until: dt.datetime | None = None
    #: For UNROUTED: why nothing will take it, when the rules can say more than
    #: "no runner" — an unbound project chat names its workspace's order and the
    #: person (`routing.project_session_waits_why`).
    reason: str = ""


def _orders_for(turns) -> dict:
    """The workspace runner orders these turns can follow: a repo turn's own
    workspace, an unbound project chat's session workspace. One query."""
    return load_workspace_orders(
        {t.workspace_id for t in turns if _is_repo_turn(t)}
        | {t.chat_session.workspace_id for t in turns if is_project_session_turn(t)}
    )


def _parked(takers) -> tuple[bool, dt.datetime | None]:
    """(every runner that could take it is paused, when the first resumes on its
    own — None if none will). The ONE parked rule, for `turn_reach` and the
    stuck-turn warning alike.

    Not "offline": waiting on a lapsed heartbeat is waiting on a laptop lid,
    waiting on a pause is waiting on somebody's decision — and only a scheduled
    unpause ends one without a person."""
    if not takers or not all(r.paused for r in takers):
        return False, None
    resumes = [r.unpause_at for r in takers if r.unpause_at is not None]
    return True, (min(resumes) if resumes else None)


def turn_reach(turn: Turn) -> Reach:
    """Will a live runner pick this turn up? Asked the moment it is enqueued, so
    the person who sent it hears "working on it" or "blocked" at once rather than
    inferring it from silence.

    Stricter than `unclaimable_queued_turns` about liveness on purpose: that one
    is an alarm and counts a DEGRADED runner as reachable so a CDP blip does not
    page anyone; this one is a promise, and `claim_next_turn` only claims on
    ONLINE, so a degraded box is not "picking it up".
    """
    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__runner_binding",
                                        "chat_session__agent", "enqueued_by")
            .get(pk=turn.pk))
    if turn.chat_session_id:
        ws = turn.chat_session.workspace_id
    elif turn.agent_id:
        ws = turn.agent.workspace_id
    else:
        ws = turn.workspace_id
    runners = [
        r for r in Runner.objects.exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
        .select_related("owner").prefetch_related("declared_flags").order_by("name")
        if ws in runner_tenant_slugs(r)
    ]
    prime_machine_units(runners)
    defaults, priorities = _assignment_rows_for_turns([turn])
    covering = [r for r, pks in _coverage({turn.pk}, runners, defaults, priorities).items() if pks]
    online = [r for r in covering if r.live_status == Runner.ONLINE]
    live = [r for r in online if not r.signed_out]
    if live:
        return Reach(LIVE, live, behind=blocking_turn(turn))
    if online:
        # Up, heartbeating, and unable to run Claude: "Picking this up on X" over
        # a box that will answer "Not logged in" is the lie this state replaces.
        return Reach(SIGNED_OUT, online)

    def blocked_by_requirements(*, live_only: bool) -> bool:
        # Would a runner take it if the conversation required nothing? Only
        # then is the requirement the reason it waits.
        if not rr.requirements_of(turn):
            return False
        without = [r for r, pks in _coverage({turn.pk}, runners, defaults, priorities,
                                             ignore_requirements=True).items() if pks]
        if live_only:
            without = [r for r in without if r.live_status == Runner.ONLINE]
        return bool(without)

    parked, held_until = _parked(covering)
    if parked:
        return Reach(PARKED, covering, blocked_by_requirements(live_only=True),
                     held_until=held_until)
    if covering:
        # Its own runners are merely offline; the requirement is the blocker
        # only if a runner that IS live would otherwise have taken it.
        return Reach(OFFLINE, covering, blocked_by_requirements(live_only=True))
    return Reach(UNROUTED, [], blocked_by_requirements(live_only=False),
                 reason=(project_session_waits_why(turn, _orders_for([turn]))
                         if is_project_session_turn(turn) else ""))


def _when(at: dt.datetime | None) -> str:
    return timezone.localtime(at, dt.timezone.utc).strftime("%b %-d %H:%M UTC") if at else "never"


def runner_situation(r: Runner) -> str:
    """One runner, in the words a stranded turn's sender and owner read: who
    paused it, when, why, and until when — or since when it was last heard
    from. The one sentence every notice about a stuck turn uses."""
    if r.paused:
        who = (r.paused_by.get_full_name() or r.paused_by.email) if r.paused_by_id else "someone"
        why = f" ({r.paused_note})" if r.paused_note else ""
        end = (f", until {_when(r.unpause_at)}" if r.unpause_at
               else ", with no time set to resume")
        return f"{r.name} was paused by {who} on {_when(r.paused_at)}{why}{end}"
    if r.signed_out and r.live_status == Runner.ONLINE:
        return f"{r.name} is online but Claude is not signed in on it"
    return f"{r.name} has not been heard from since {_when(r.last_heartbeat_at)}"


def queued_turns_covered_by(runner: Runner) -> list[Turn]:
    """The QUEUED turns this runner is one of the takers for — the same coverage
    `turn_reach` and the claim use, so a pause re-checks exactly the turns it
    could have stranded."""
    queued = list(Turn.objects.filter(status=Turn.QUEUED).select_related(
        "agent", "chat_session", "chat_session__runner_binding", "chat_session__agent"))
    if not queued:
        return []
    defaults, priorities = _assignment_rows_for_turns(queued)
    covered = _coverage({t.pk for t in queued}, [runner], defaults, priorities).get(runner, set())
    return [t for t in queued if t.pk in covered]


def _readable_session_turn_q(user) -> Q:
    """Turns that are not session turns, or are session turns in a chat `user`
    may read. No user (a contact's view): no extra narrowing — the caller passes
    its own `turn_q`."""
    if user is None:
        return Q()
    from apps.canopy_sessions import access as session_access
    from apps.canopy_sessions.models import Session

    readable = Session.objects.filter(session_access.visible_session_q(user)).values("pk")
    return Q(chat_session__isnull=True) | Q(chat_session__in=readable)


def unclaimable_queued_turns(user=None, *, ws_slugs=None, turn_q=None) -> list[dict]:
    """Queued turns that look genuinely stuck — otherwise a silent stall.

    enqueue_turn accepts a turn addressed to an agent/repo nothing declares, and it
    then sits QUEUED forever with no signal (observed: a project=ace turn sat 12h).

    Two DIFFERENT causes, reported differently because they need different actions:
      * config    — no runner can target this turn at all (agent unassigned, repo
                    undeclared, session bound to a runner you don't have). It will
                    never run until routing is edited.
      * offline   — a runner can target it, but none are reachable right now.
                    Usually transient (network blip, deploy, laptop asleep).
    Returns [{turn_id, target, prompt, created_at, reason, kind}].
    """
    # A user's view: every tenant they belong to. A CONTACT's: their one
    # workspace, narrowed by `turn_q` to their own conversations.
    if ws_slugs is None:
        ws_slugs = wsvc.user_workspace_slugs(user)
    if not ws_slugs:
        return []
    cutoff = timezone.now() - UNCLAIMABLE_GRACE
    queued = list(
        Turn.objects.filter(status=Turn.QUEUED, created_at__lte=cutoff)
        .filter(
            (Q(agent__isnull=False) & agent_tenant_q(ws_slugs))
            | (Q(agent__isnull=True) & Q(chat_session__isnull=True) & Q(workspace_id__in=ws_slugs))
            | (Q(chat_session__isnull=False) & Q(chat_session__workspace_id__in=ws_slugs))
        )
        # chat_session + its binding are read per turn by the per-source
        # refinement below (once per turn PER RUNNER), so preload them.
        .select_related("agent", "chat_session", "chat_session__runner_binding",
                        "chat_session__agent", "enqueued_by", "pinned_runner")
        .filter(turn_q if turn_q is not None else Q())
        # A user sees a session turn (and its prompt) only in a chat they may
        # read — the chat ACL, not the tenant. A contact is already narrowed to
        # their own conversations by `turn_q`.
        .filter(_readable_session_turn_q(user))
        .order_by("created_at")
    )
    if not queued:
        return []
    # While the fleet is held every queued turn waits on that, and nothing else about
    # its routing matters until the release — say so instead of a routing diagnosis.
    hold = FleetHold.objects.filter(pk=FleetHold.SINGLETON_PK, held=True).first()
    if hold is not None:
        from . import turn_access

        why = f"the fleet is on hold{f' ({hold.note})' if hold.note else ''} — no runner claims anything until it is released"
        return [{
            "turn_id": str(t.pk),
            "target": (f"agent {t.agent.slug}" if t.agent_id else
                       "session" if t.chat_session_id else f"project {t.project}"),
            "prompt": ((t.prompt or "")[:120]
                       if user is None or turn_access.can_read_turn_content(user, t) else ""),
            "created_at": t.created_at, "reason": why, "kind": "hold",
        } for t in queued]
    # Candidate runners for "could ANY runner take this?" are the runners VISIBLE
    # in the caller's tenant, not merely the ones the caller personally paired.
    # Scoping to `owner=user` made every stuck turn read as `config` for
    # anyone who didn't pair a runner themselves (a delegated identity, or a
    # teammate in a workspace someone else's runner serves) — the workspace's
    # runner could be sitting right there, offline, and the diagnosis would still
    # say "no runner is assigned; fix your routing." Use the SAME tenancy rule as
    # claim_next_turn (`runner_tenant_slugs`, owner-derived, NULL-fails-closed)
    # so this warning can't disagree with what claiming actually does.
    runners = [
        r for r in Runner.objects.exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
        .select_related("owner").prefetch_related("declared_flags")
        if runner_tenant_slugs(r) & ws_slugs
    ]
    prime_machine_units(runners)
    ids = {t.id for t in queued}
    defaults, priorities = _assignment_rows_for_turns(queued)

    reachable = [r for r in runners if r.live_status in (Runner.ONLINE, Runner.DEGRADED)]
    claimable_now = set().union(*_coverage(ids, reachable, defaults, priorities).values())
    # Would ANY paired runner take it if it were up? Separates "misconfigured" from
    # "temporarily unreachable" — the difference between "fix the routing matrix"
    # and "wait, or check the runner".
    ever_by_runner = _coverage(ids, runners, defaults, priorities)
    claimable_ever = set().union(*ever_by_runner.values())
    # Counterfactual, for turns whose conversation requires something and that no
    # runner will ever take: would one take it WITHOUT the requirement? Only then
    # is the requirement the blocker; otherwise the ordinary reason applies.
    required = {t.pk for t in queued if t.pk not in claimable_ever and rr.requirements_of(t)}
    blocked_by_reqs = set().union(*_coverage(required, runners, defaults, priorities,
                                             ignore_requirements=True).values()) if required else set()
    # The same counterfactual for the hold gate: a turn only boxes whose owners
    # are not admins of its agent would take is a grant away, not a routing fix.
    unheld = {t.pk for t in queued if t.pk not in claimable_ever and t.acting_agent is not None}
    blocked_by_hold = set().union(*_coverage(unheld, runners, defaults, priorities,
                                             ignore_hold=True).values()) if unheld else set()
    # And for a box's own report that it cannot run the agent: a turn only such
    # boxes would take is a setup gap on those boxes, named with what each said.
    # Its isolation unit counts here too, for every turn (an agent unit refuses
    # the agentless ones as well).
    unserved = {t.pk for t in queued if t.pk not in claimable_ever}
    by_unserving = _coverage(unserved, runners, defaults, priorities,
                             ignore_serve=True) if unserved else {}
    orders = _orders_for(queued)

    out = []
    for t in queued:
        if t.pk in claimable_now:
            continue
        if t.chat_session_id:
            target, what = "session", "can take this session (session-capable + its binding)"
        elif t.agent_id:
            target, what = f"agent {t.agent.slug}", f"routes the agent '{t.agent.slug}' (by its own runners or its workspace's default order)"
        else:
            target, what = f"project {t.project}", f"declares the repo '{t.project}'"
        reqs = rr.requirements_of(t)
        pin = t.pinned_runner if t.pinned_runner_id else None
        if pin is not None and t.pk not in claimable_ever and (
                pin.status == Runner.RETIRED or pin not in runners):
            # A pin trumps routing, so "no runner routes the agent" sends its
            # owner to fix routing that is not broken: the turn waits for ONE box.
            kind = "config"
            reason = (f"pinned to {pin.name}, which has been retired — it will never run"
                      if pin.status == Runner.RETIRED else
                      f"pinned to {pin.name}, which is not in this workspace — it will never run")
        elif reqs and t.pk in blocked_by_reqs:
            kind = "config"
            reason = (f"this conversation's site requires a {rr.describe(reqs)} runner, and "
                      f"no runner that serves it is declared {rr.describe(reqs)}")
        elif said := _unserving_said(t, by_unserving):
            kind = "config"
            if t.acting_agent is not None:
                reason = (f"the runners that {what} report they cannot run "
                          f"'{t.acting_agent.slug}': {said} — set it up on that box and "
                          "refresh the runner, or route the agent to a box that has it")
            else:
                reason = (f"the runners that {what} cannot take it: {said} — an agent "
                          "unit runs only its own agent's work")
        elif t.pk in blocked_by_hold:
            kind = "config"
            reason = (f"the runners {what} are owned by people who are not admins of "
                      f"'{t.acting_agent.slug}', so they may not run as it — make the "
                      "runner's owner an admin of the agent")
        elif t.capability and t.pk not in claimable_ever:
            kind = "config"
            reason = (f"this is a caller's turn, confined to '{t.capability}', and no runner "
                      "that can confine one serves it — update the runner and the canopy "
                      "plugin on a box assigned to it")
        elif t.pk in claimable_ever and _parked(takers := [
                r for r, pks in ever_by_runner.items() if t.pk in pks])[0]:
            kind = "parked"
            reason = (f"pinned to one runner: {runner_situation(pin)}" if pin is not None else
                      f"every runner that {what} is paused: " + "; ".join(
                          runner_situation(r) for r in takers))
        elif t.pk in claimable_ever:
            kind = "offline"
            reason = (f"pinned to one runner: {runner_situation(pin)}" if pin is not None else
                      f"a runner {what}, but none are reachable right now (offline or heartbeat lapsed)")
        elif is_project_session_turn(t):
            kind = "config"
            reason = project_session_waits_why(t, orders)
        else:
            kind = "config"
            reason = f"no runner {what}"
        from . import turn_access

        out.append({
            "turn_id": str(t.pk), "target": target,
            # A prompt is a log (turn_access); a contact's view is already
            # narrowed to their own conversations by `turn_q`.
            "prompt": ((t.prompt or "")[:120]
                       if user is None or turn_access.can_read_turn_content(user, t) else ""),
            "created_at": t.created_at, "reason": reason, "kind": kind,
        })
    return out


def _unserving_said(t: Turn, by_unserving: dict) -> str:
    """What each runner that would take `t` but for its own report says about it:
    its isolation unit (`turn_unit_refusal`), else `cannot_serve` for the turn's
    agent. "" when none would."""
    agent = t.acting_agent
    said = []
    for r, pks in by_unserving.items():
        if t.pk not in pks:
            continue
        why = turn_unit_refusal(r, t) or (cannot_serve(r, agent.slug) if agent is not None else "")
        if why:
            said.append(why)
    return "; ".join(said)


def _capability_sweep() -> None:
    """apps/capabilities' sweep, behind a guard of its own: claiming must not
    depend on it even if its import fails."""
    try:
        from apps.capabilities.services import sweep_tick

        sweep_tick(trigger="claim")
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("capability sweep hook failed")


def claim_next_turn(runner: Runner, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    exclude_slugs: list[str] | None = None, acts_as: str = "") -> Turn | None:
    """`acts_as`: the lower mode the runner claims in (role_cap) — stamped on a turn
    its OWNER started, so that turn runs as that role; anyone else's turn keeps its own."""
    # Cloud-capability leases: an EXTRA trigger for their sweep, ABOVE every early
    # return — a fleet hold or a paused runner stops turns, never a machine's
    # expiry. The sweep has its own clock in every web process
    # (apps/capabilities/scheduler.py); this runs it only when no sweep has
    # finished for SWEEP_STALE_S (the clock is not keeping up), so normally it is
    # one cache read and no AWS call rides the claim. It never raises.
    _capability_sweep()
    from apps.workspaces import role_cap

    runner_cap = role_cap.cap_from(acts_as)
    # Covers the operator PAUSE too: `live_status` returns PAUSED for a parked
    # runner, so this one guard is the whole server-side enforcement. Spelled out
    # because it is easy to read this line as a pure liveness check and then
    # "helpfully" add a pause bypass somewhere below.
    #
    # Note WHERE it sits: above pin matching, so a pause outranks a pin. Both are
    # operator intent, but the pause is the more specific and more recent one, and
    # a pin that could resurrect a parked box would re-open exactly the hole pause
    # closes — work landing on an account that must not spend tokens. The pinned
    # turn stays QUEUED and lands on unpause (only a readiness drill expires:
    # `expire_stale_drills`).
    if runner.live_status != Runner.ONLINE:
        return None
    # A box whose Claude login is gone runs nothing: every turn it took would come
    # back as the CLI's "Not logged in" (2026-10-22, acedimagi-mbp-cdp answered
    # three messages that way). Not every not-ready note means that — a box with
    # no Claude desktop CLI still runs turns in emdash — so only this one gates.
    # Above the pin, like the pause: a pin cannot make a signed-out box work.
    if runner.signed_out:
        return None
    # A computer used in plain Claude Code has no runner app to run a claimed turn;
    # it never heartbeats, so the guard above already stops it — this says so.
    if runner.kind == Runner.COMPUTER:
        return None
    # The FLEET-WIDE hold (FleetHold): nothing is claimed, ridden or pinned anywhere
    # while it is on, and queued turns wait — visible, with their trigger — for the
    # release. ABOVE the sweeps on purpose: `skip_late_scheduled_turns` would close a
    # held schedule slot as MISSED after 30 minutes, erasing it from the very queue the
    # hold exists to show. On release the first claim runs the sweeps, so stale slots
    # close then instead of firing as a burst. Above pin matching for the same reason
    # the runner pause is: a pin must not punch through an operator's stop.
    if FleetHold.is_held():
        return None
    sweep_expired_leases()
    # Lazy sweeps, both BEFORE the busy_agents read: a turn released here frees
    # its agent for the very claim we are about to make.
    release_stale_occurrence_turns_all()
    skip_late_scheduled_turns()
    expire_stale_drills()
    projects = runner.project_names()
    session_capable = runner.session_capable()
    has_pins = Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner).exists()
    # A workspace's order counts: agents with no order of their own follow it,
    # so a box named only there still has agent work to look for.
    has_assignments = (
        RunnerAssignment.objects.filter(runner=runner).exists()
        or WorkspaceRunnerOrder.objects.filter(runner=runner, enabled=True).exists()
    )
    if runner.directed_only():
        # Takes only what is put on it: a pin, or a session bound to it.
        from apps.canopy_sessions.models import RunnerBinding

        if not has_pins and not RunnerBinding.objects.filter(runner=runner).exists():
            return None
    elif not has_assignments and not projects and not session_capable and not has_pins:
        return None
    routing_q = Q(routing__in=[Turn.PREFER_LOCAL, Turn.LOCAL_ONLY, Turn.ANY])
    if runner.kind == Runner.CLOUD:
        routing_q = Q(routing=Turn.ANY) | Q(routing=Turn.PREFER_LOCAL)
        # prefer_local turns fall to cloud only via the Phase 2 router policy;
        # Phase 0 has no cloud runners, so keep the simple rule: cloud never
        # takes local_only.
    # Both busy sets live in `busy_agent_ids` / `busy_session_ids` (with why
    # their null filters are load-bearing), shared with `blocking_turn` so the
    # status line's "queued behind the current turn" is this exclusion, not a
    # second guess at it.
    busy_agents = busy_agent_ids()
    # A conversation this runner is running is not "busy" to it when it can
    # deliver the follow-up into that turn (`may_ride`, canopy-web#1153).
    ridable = ridable_sessions(runner)
    busy_sessions = busy_session_ids().exclude(chat_session_id__in=list(ridable))
    # Tenant boundary. capabilities is a caller-supplied routing hint declared at
    # pairing and never validated (b4f5ead, Critical); the workspace is the actual
    # gate, and the two INTERSECT — one never substitutes for the other.
    #
    # The slug set and the agent predicate both come from the SHARED helpers
    # (runner_tenant_slugs / agent_tenant_q, above), which `_runner_schedule_qs`
    # in api.py also calls. That is not tidiness: these two rules diverging is
    # the 2026-07-25 outage (see runner_tenant_slugs' docstring). Sharing the
    # definition is what makes "every schedule this runner may fire produces a
    # turn this runner may claim" hold by construction rather than by two
    # comments agreeing with each other; tests/test_claim_schedule_parity.py
    # pins the behaviour end to end.
    #
    # The b4f5ead exploit stays closed: owner is server-assigned from
    # request.user at pairing, so unlike capabilities it is not attacker-
    # controlled. An outsider pairing a runner that declares a victim's agent
    # slug gets only THEIR OWN workspaces, so the victim's agent stays
    # unclaimable. Conversely a runner owned by someone who is a member of a
    # workspace may claim its agents' turns — that human can already drive those
    # agents through the UI, so there is no escalation.
    ws_slugs = runner_tenant_slugs(runner)
    # Three target kinds, each tenant-gated on its own workspace source: agent
    # turns via agent.workspace; project turns via their own workspace FK; session
    # turns via chat_session.workspace. NONE of them has a null-workspace escape
    # hatch any more — the agent leg's went away with agents/0013 (NOT NULL), and
    # the project/session legs never had one. Splitting by target kind stays
    # load-bearing regardless: `agent__workspace_id` traverses a nullable FK, so a
    # single combined clause would evaluate against NULL for project/session turns.
    tenant_q = (
        (Q(agent__isnull=False) & agent_tenant_q(ws_slugs))
        | (Q(agent__isnull=True) & Q(chat_session__isnull=True) & Q(workspace_id__in=ws_slugs))
        | (Q(chat_session__isnull=False) & Q(chat_session__workspace_id__in=ws_slugs))
    )
    # `busy_agents` serializes AGENTS only, and a project turn (agent_id NULL)
    # must not be swept up by it. This plain exclude() is correct: Django compiles
    # it to `NOT (agent_id IN (…) AND agent_id IS NOT NULL)`, so NULL-agent rows
    # survive rather than falling into SQL's NULL-propagation trap. Verified by
    # test_a_busy_agent_does_not_block_a_project_turn, which is what makes it safe
    # to rely on.
    # Target match: this runner's declared agents/projects, plus every session
    # turn when it is session-capable (a chat send targets no specific agent — any
    # session-capable runner in the tenant may take it).
    target_q = runner_target_q(runner, exclude_slugs)
    # A pin trumps target/routing matching (but NOTHING else): a turn pinned to
    # this runner is claimable even with empty capabilities — that is what lets a
    # warm standby be drilled. A turn pinned elsewhere is invisible. Note the pin
    # arm also bypasses exclude_slugs (the per-agent LOCAL pause) — deliberately:
    # a pin is a drill or an explicit placement, i.e. operator intent, and that
    # intent should not be silently swallowed by a pause the operator set for
    # unrelated routed traffic on the same agent.
    match_q = Q(pinned_runner=runner) | (target_q & routing_q)
    candidates = list(
        Turn.objects.filter(status=Turn.QUEUED)
        .filter(Q(pinned_runner__isnull=True) | Q(pinned_runner=runner))
        .filter(match_q)
        .filter(profile_q(runner))
        .exclude(agent_id__in=busy_agents)
        .exclude(chat_session_id__in=busy_sessions)
        .filter(tenant_q)
        # _assignment_allows reads turn.agent_id; the session leg's stickiness
        # check reads chat_session.agent_id + chat_session.runner_binding.
        .select_related("agent", "chat_session", "chat_session__runner_binding",
                        "chat_session__agent", "enqueued_by")
        .order_by("created_at")
    )
    # Two-pass: materialize candidates above, then batch-load every candidate
    # agent's ranked assignment list in one query rather than per-turn. Includes
    # session agents so the cascade check below can look them up too.
    agent_ids = {t.agent_id for t in candidates if t.agent_id} | {
        t.chat_session.agent_id for t in candidates if t.chat_session_id and t.chat_session.agent_id
    }
    # One query for every candidate agent's rows, split into the default list and
    # the per-source priorities; the per-turn composition below is in-memory.
    # enabled=True only, filtered inside the loader: a disabled row must neither
    # claim (it is absent from the composed list, so `mine` comes back None) nor
    # count as a better-ranked availability blocker for a lower enabled rank.
    defaults, priorities = load_assignment_rows(agent_ids)
    # The workspace runner order, for the repo turns and unbound project chats
    # among the candidates — the ranking a project dispatch never had
    # (WorkspaceRunnerOrder).
    orders = _orders_for(candidates)
    admins: dict = {}
    now = timezone.now()
    my_flags = runner.flags
    from apps.agents.services import runner_may_hold_agent

    trusted: dict[int, bool] = {}
    for turn in candidates:
        # Above the pin on purpose, like profile_q: a pin is a placement, never a
        # way past what the conversation's host requires of the box.
        if not rr.satisfies(my_flags, rr.requirements_of(turn)):
            continue
        # An agent turn runs AS the agent: its prompt, its caller's token, its
        # owner's GitHub identity. Only a box whose owner is one of the agent's
        # admins may take one — also above the pin, since pinning is open to the
        # editor tier and must not be a way to direct an agent at your own box.
        # A chat with an agent runs as that agent just the same (its session,
        # its credentials), so a session turn is held to its agent's rule too.
        held = turn.acting_agent
        if held is not None:
            if held.id not in trusted:
                trusted[held.id] = runner_may_hold_agent(runner, held)
            if not trusted[held.id]:
                continue
        # Its isolation unit, above the pin and the binding like the hold: an agent
        # unit runs only turns that act as its agent (none of a repo's or an
        # agentless chat's), and an agent with a unit of its own runs on no other
        # unit of the machine — a pinned or bound turn would otherwise hand it the
        # agent's secrets (`device_keys.runner_serves_agent`).
        if turn_unit_refusal(runner, turn):
            continue
        pinned_here = turn.pinned_runner_id == runner.id
        if not pinned_here:
            if not _kind_allows(runner, turn.routing):
                continue
            if turn.agent_id:
                if not _assignment_allows(runner, turn, defaults, priorities, now):
                    continue
            if _is_repo_turn(turn) and not _repo_order_allows(runner, turn, orders, now):
                continue
            # An unbound project chat: a box with its repo, that the workspace's
            # order shares or the chatting person administers (spec 2026-10-19).
            if is_project_session_turn(turn) and not project_session_allows(
                    runner, turn, orders, now, admins=admins):
                continue
            if turn.chat_session_id:
                sess = turn.chat_session
                binding = getattr(sess, "runner_binding", None)
                bound_to_me = binding is not None and binding.runner_id == runner.id
                if not bound_to_me and sess.agent_id:
                    if not _assignment_allows_for_agent(
                        runner, sess.agent_id, turn, defaults, priorities, now
                    ):
                        continue
        # The mode is decided HERE, once, from the same rules that just routed
        # the turn (apps/harness/turn_mode.py), and stamped so a rule edited
        # mid-turn cannot flip the posture of work already under way.
        from . import turn_mode as modes

        # A turn started as a lower role (canopy-web#1705) is judged at that role.
        turn_cap = role_cap.cap_from((turn.origin_ref or {}).get("acts_as"))
        stamp = ""
        if runner_cap and turn.initiator_user_id and turn.initiator_user_id == runner.owner_id:
            # The runner claims in a lower mode: its owner's turn runs in it too
            # (never loosening a cap the turn was started with).
            stamp = role_cap.stronger(turn_cap, runner_cap)
            if stamp != turn_cap:
                turn.origin_ref = {**(turn.origin_ref or {}), "acts_as": stamp}
            else:
                stamp = ""
        acts_as = stamp or turn_cap
        mode = modes.for_turn(turn, priorities, fresh=True)
        if acts_as and mode is not None and mode.mode == modes.AUTO:
            # Only an agent admin may run auto, and a capped initiator is not one.
            mode = modes.Resolved(modes.MANUAL, f"started as {acts_as}: manual")
        # Admission rules (apps/admission): the one point where the runner's kind
        # and the resolved mode are both known. An `auto` rule may first raise a
        # standing manual to auto on this runner; then a rejected turn is closed
        # CANCELLED with its notices owed, and never reaches a session.
        from apps.admission import evaluate as admission

        auto = admission.auto_rule(turn, runner, mode)
        if auto is not None:
            mode = modes.Resolved(modes.AUTO, admission.auto_basis(auto))
        if admission.at_claim(turn, runner, mode.mode if mode else ""):
            continue
        rides = ridable.get(turn.chat_session_id) if turn.chat_session_id else None
        if rides is not None and (turn.capability or (turn.origin_ref or {}).get(MIDTURN_FAILED)):
            # A confined turn never rides into the owner's session; one whose
            # mid-turn delivery already failed waits for the turn to end.
            continue
        try:
            # Own atomic block per attempt: an IntegrityError from the
            # one_executing_turn_per_agent index (concurrent claim for the
            # same agent) must not poison an outer transaction.
            with transaction.atomic():
                updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
                    status=Turn.CLAIMED,
                    claimed_by=runner,
                    claimed_at=now,
                    lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
                    turn_mode=mode.mode if mode else "",
                    turn_mode_basis=mode.basis[:320] if mode else "",
                    rides_turn_id=rides,
                    **({"origin_ref": turn.origin_ref} if stamp else {}),
                )
        except IntegrityError:
            continue  # another runner claimed for this agent between our check and update
        if updated:
            turn.refresh_from_db()
            if auto is not None:
                admission.record_auto(turn, auto, runner)
            append_events(turn, [{"kind": "status", "payload": {
                "status": Turn.CLAIMED, "runner": runner.name,
                **_routing_basis(turn, runner, priorities),
                **({"rides_turn": str(rides)} if rides else {}),
            }}])
            return turn
    return None


def _routing_basis(turn: Turn, runner: Runner, priorities: dict) -> dict:
    """WHY this runner got this turn, recorded on the claim event: the derived
    actor and the rung that matched. Without it a routing audit has to re-derive
    the ladder from rules that may have changed since — and with several boxes
    serving one agent, "why did my turn land there?" is the question people ask."""
    from .actors import actor_of

    if turn.pinned_runner_id == runner.id:
        return {"actor": "", "rule": "pin"}
    actor = actor_of(turn)
    if turn.agent_id and actor and priorities.get((turn.agent_id, turn.origin, actor)):
        rule = "actor"
    elif turn.agent_id and priorities.get((turn.agent_id, turn.origin, "")):
        rule = "source"
    elif turn.chat_session_id and not turn.agent_id:
        rule = "session"
    else:
        rule = "default"
    return {"actor": actor, "rule": rule}
