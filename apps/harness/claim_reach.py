"""What the claim would do, read-only: will a live runner take this turn, and why not.

`turn_reach` (what the turn status and Slack say), `unclaimable_queued_turns` (the web's
stuck-turn warning) and `runner_situation` apply the same routing rules `claim_next_turn`
applies (claim.py), so the claim and its explanations cannot drift apart. Split out of
claim.py; nothing here writes.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.agents.holding import runner_may_hold_agent
from apps.canopy_sessions.models import Session
from apps.harness import actors, clis
from apps.harness import runner_requirements as rr
from apps.workspaces import services as wsvc

from .models import (
    FleetHold,
    Runner,
    Turn,
)
from .routing import (
    agent_tenant_q,
    assignment_rows_for,
    blocking_turn,
    cannot_serve,
    is_job_gated,
    is_project_session_turn,
    is_repo_turn,
    job_refusal,
    load_assignment_rows,
    load_workspace_orders,
    prime_machine_runners,
    profile_q,
    project_session_covers,
    project_session_waits_why,
    repo_order_for,
    routed_slug,
    runner_target_q,
    runner_tenant_slugs_of,
    transfer_holding,
    turn_serves_refusal,
)

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
    if holds is None:
        return runner_may_hold_agent(r, agent)
    key = (r.id, agent.id)
    if key not in holds:
        holds[key] = runner_may_hold_agent(r, agent)
    return holds[key]


def _refined_allows(r: Runner, t: Turn, defaults: dict, priorities: dict,
                    *, ignore_requirements: bool = False, orders: dict | None = None,
                    ignore_hold: bool = False, holds: dict | None = None,
                    ignore_serve: bool = False, admins: dict | None = None,
                    ignore_cli: bool = False) -> bool:
    """The per-candidate refinements claim_next_turn applies after the coarse
    target match — same checks, same ORDER, so coverage can't overstate what
    claiming will do.

    `ignore_requirements` answers the counterfactual "would this runner take it
    if the conversation required nothing?" — only ever asked to decide whether
    a requirement is what BLOCKS a turn, never to route one. `ignore_hold` is
    the same counterfactual for `runner_may_hold_agent`, and `ignore_serve` for a
    box's own report that it cannot run the agent (`routing.cannot_serve`), and
    `ignore_cli` for the agent CLI (`clis.choose`). Sign-in is not asked here: like
    a box's liveness, `turn_reach` reads it on top (a signed-out box still covers)."""
    reqs = frozenset() if ignore_requirements else rr.requirements_of(t)
    if not rr.runner_satisfies(r, reqs):
        return False  # above the pin and the binding, as in claim_next_turn
    if job_refusal(r, t) is not None:
        return False  # a job: only a cloud box that runs them, pin or no pin
    if not ignore_hold and not _may_hold(r, t, holds):
        return False  # likewise above the pin and the binding
    if not ignore_serve and turn_serves_refusal(r, t):
        return False  # what the runner serves on its machine: above the pin and the binding too
    if not ignore_cli and not t.job and clis.choose(t, r, ignore_signed_in=True,
                                      ignore_requirements=ignore_requirements) is None:
        return False  # the agent CLI: above the pin, as in claim_next_turn
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
        if is_repo_turn(t):
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
              ignore_serve: bool = False, ignore_cli: bool = False) -> dict:
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
        q = runner_target_q(r, ignore_serves=ignore_serve) | Q(pinned_runner=r)
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
                               ignore_serve=ignore_serve, admins=admins,
                               ignore_cli=ignore_cli):
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
    #: True only when the conversation's runner requirements (`sensitive_data`) are what
    #: keeps it from a live runner: some runner would take it without them. A
    #: turn stuck for another reason (no routing, a box that cannot confine a
    #: caller's turn) must not be blamed on them — that sends its owner to the
    #: wrong fix.
    blocked_by_requirements: bool = False
    #: The turn this one waits behind (`blocking_turn`): its session's previous
    #: message, or a transfer's turn still waiting to start. A LIVE runner will
    #: take it, but not until that turn ends — "picking this up" would be a
    #: promise the claim is not keeping.
    behind: Turn | None = None
    #: Its session is mid-transfer and the transfer turn does not exist yet (the
    #: source is still packing its work, `routing.transfer_holding`).
    transfer: bool = False
    #: For PARKED: when the first of its runners resumes on its own (a scheduled
    #: unpause), or None when only a person resuming one will run it.
    held_until: dt.datetime | None = None
    #: For UNROUTED: why nothing will take it, when the rules can say more than
    #: "no runner" — an unbound project chat names its workspace's order and the
    #: person (`routing.project_session_waits_why`).
    reason: str = ""
    #: For SIGNED_OUT: the agent CLI none of its runners has signed in ("codex").
    cli: str = ""


def _orders_for(turns) -> dict:
    """The workspace runner orders these turns can follow: a repo turn's own
    workspace, an unbound project chat's session workspace. One query."""
    return load_workspace_orders(
        {t.workspace_id for t in turns if is_repo_turn(t)}
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
    if (held := turn.acting_agent) is not None and held.held:
        # The agent hold: no runner claims it until release, whatever routing says.
        return Reach(UNROUTED, [], reason=f"the agent '{held.slug}' is on hold")
    if turn.chat_session_id:
        ws = turn.chat_session.workspace_id
    elif turn.agent_id:
        ws = turn.agent.workspace_id
    else:
        ws = turn.workspace_id
    candidates = list(Runner.objects.exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
                      .select_related("owner").prefetch_related("declared_flags", "enrollments").order_by("name"))
    tenants = runner_tenant_slugs_of(candidates)
    runners = [r for r in candidates if ws in tenants[r.pk]]
    prime_machine_runners(runners)
    defaults, priorities = _assignment_rows_for_turns([turn])
    covering = [r for r, pks in _coverage({turn.pk}, runners, defaults, priorities).items() if pks]
    online = [r for r in covering if r.live_status == Runner.ONLINE]
    # Signed in to a CLI this turn may run on (`clis.choose`, which coverage asked
    # without the sign-in): an online box whose Codex login is gone does not pick
    # up a Codex conversation, whatever its Claude login says.
    live = [r for r in online if clis.choose(turn, r) is not None]
    # A fleet member that is in the warm pool (or coming into service) is on its
    # way: the scaler grows the fleet for this turn (`fleets.tick`), so it is a
    # taker, not "offline".
    live += [r for r in covering if r.wakeable and r not in live]
    if live:
        return Reach(LIVE, live, behind=blocking_turn(turn),
                     transfer=transfer_holding(turn) is not None)
    if online:
        # Up, heartbeating, and unable to run the turn's CLI: "Picking this up on X"
        # over a box that will answer "Not logged in" is the lie this state replaces.
        # The CLI to name is the one a box would run it on were it signed in.
        cli = next((c for r in online
                    if (c := clis.choose(turn, r, ignore_signed_in=True))), "") or clis.CLAUDE_CODE
        return Reach(SIGNED_OUT, online, cli=cli,
                     reason=f"no runner has {clis.label(cli)} signed in")

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


def _cli_may_block(queued, unserved: set, runners) -> bool:
    """Whether the agent CLI can be what keeps any of `unserved` waiting: some
    waiting turn may run on a CLI other than Claude Code (or on none), or some box
    lacks Claude Code. False means the CLI pass would find nothing."""
    if any(clis.CLAUDE_CODE not in clis.reported(r) for r in runners):
        return True
    return any(clis.allowed(t) != [clis.CLAUDE_CODE] for t in queued if t.pk in unserved)


def _when(at: dt.datetime | None) -> str:
    return timezone.localtime(at, dt.UTC).strftime("%b %-d %H:%M UTC") if at else "never"


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
    candidates = list(Runner.objects.exclude(status=Runner.RETIRED).exclude(kind=Runner.COMPUTER)
                      .select_related("owner").prefetch_related("declared_flags", "enrollments"))
    tenants = runner_tenant_slugs_of(candidates)
    runners = [r for r in candidates if tenants[r.pk] & ws_slugs]
    prime_machine_runners(runners)
    ids = {t.id for t in queued}
    defaults, priorities = _assignment_rows_for_turns(queued)

    reachable = [r for r in runners if r.live_status in (Runner.ONLINE, Runner.DEGRADED) or r.wakeable]
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
    # What it serves on its machine counts here too, for every turn (an agent's runner refuses
    # the agentless ones as well).
    unserved = {t.pk for t in queued if t.pk not in claimable_ever}
    by_unserving = _coverage(unserved, runners, defaults, priorities,
                             ignore_serve=True) if unserved else {}
    # And for the agent CLI: a turn only boxes without the CLI it runs on would
    # take is a box to set that CLI up on, not routing to fix (`clis.choose`).
    # Skipped where it can find nothing new — every waiting turn Claude Code only,
    # on boxes that all have it — so a Claude-only fleet pays no extra pass.
    by_cli = (_coverage(unserved, runners, defaults, priorities, ignore_cli=True)
              if unserved and _cli_may_block(queued, unserved, runners) else {})
    blocked_by_cli = set().union(*by_cli.values()) if by_cli else set()
    orders = _orders_for(queued)

    out = []
    held_ws = wsvc.held_slugs()
    for t in queued:
        if held_ws and (slug := turn_workspace_slug(t)) in held_ws:
            # Like the agent hold below: routing is beside the point until release.
            holder = wsvc.held_by(slug)
            name = holder.slug if holder is not None else slug
            note = f" ({holder.held_note})" if holder is not None and holder.held_note else ""
            out.append(_unclaimable_row(user, t, f"workspace {name}", "workspace_hold",
                                        f"the workspace '{name}' is on hold{note} — nothing in it "
                                        "runs until it is released"))
            continue
        if (held := t.acting_agent) is not None and held.held:
            # Routing is beside the point until the agent hold is released — and
            # coverage does not know the hold, so this comes before `claimable_now`.
            note = f" ({held.held_note})" if held.held_note else ""
            out.append(_unclaimable_row(user, t, f"agent {held.slug}", "agent_hold",
                                        f"the agent '{held.slug}' is on hold{note} — none of its "
                                        "turns run until it is released"))
            continue
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
                      "no runner that serves it is declared one")
        elif said := _unserving_said(t, by_unserving):
            kind = "config"
            if t.acting_agent is not None:
                reason = (f"the runners that {what} report they cannot run "
                          f"'{t.acting_agent.slug}': {said} — set it up on that box and "
                          "refresh the runner, or route the agent to a box that has it")
            else:
                reason = (f"the runners that {what} cannot take it: {said} — an agent's "
                          "runner runs only its own agent's work")
        elif t.pk in blocked_by_hold:
            kind = "config"
            reason = (f"the runners {what} are owned by people who are not admins of "
                      f"'{t.acting_agent.slug}', so they may not run as it — make the "
                      "runner's owner an admin of the agent")
        elif t.pk in blocked_by_cli:
            kind = "config"
            reason = clis.blocker(t, [r for r, pks in by_cli.items() if t.pk in pks])
        elif is_job_gated(t) and t.pk not in claimable_ever:
            kind = "config"
            reason = ("No cloud machine that can run jobs serves this agent — set one up "
                      "and add it to the agent's runners, or change the schedule.")
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
        out.append(_unclaimable_row(user, t, target, kind, reason))
    return out


def _unclaimable_row(user, t: Turn, target: str, kind: str, reason: str) -> dict:
    from . import turn_access

    return {
        "turn_id": str(t.pk), "target": target,
        # A prompt is a log (turn_access); a contact's view is already
        # narrowed to their own conversations by `turn_q`.
        "prompt": ((t.prompt or "")[:120]
                   if user is None or turn_access.can_read_turn_content(user, t) else ""),
        "created_at": t.created_at, "reason": reason, "kind": kind,
    }


def _unserving_said(t: Turn, by_unserving: dict) -> str:
    """What each runner that would take `t` but for its own report says about it:
    what it serves on its machine (`turn_serves_refusal`), else `cannot_serve` for the turn's
    agent. "" when none would."""
    agent = t.acting_agent
    said = []
    for r, pks in by_unserving.items():
        if t.pk not in pks:
            continue
        why = turn_serves_refusal(r, t) or (cannot_serve(r, agent.slug) if agent is not None else "")
        if why:
            said.append(why)
    return "; ".join(said)


def turn_workspace_slug(turn: Turn) -> str | None:
    """The workspace a turn belongs to, by the same three sources."""
    if turn.agent_id:
        return turn.agent.workspace_id
    if turn.chat_session_id:
        return turn.chat_session.workspace_id
    return turn.workspace_id
