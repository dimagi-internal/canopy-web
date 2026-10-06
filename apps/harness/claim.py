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
    Runner,
    RunnerAssignment,
    RunnerDrill,
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
    load_assignment_rows,
    load_workspace_orders,
    profile_q,
    repo_order_for,
    ridable_sessions,
    runner_target_q,
    runner_tenant_slugs,
)
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
        updated = Turn.objects.filter(pk=turn.pk, lease_expires_at__lt=now).exclude(
            status__in=Turn.TERMINAL
        ).update(status=status, finished_at=now)
        if updated:
            append_events(turn, [{"kind": "status", "payload": {"status": status, "reason": "lease_expired"}}])
            count += 1
            # A LOST/CANCELLED turn is marked via a queryset update, bypassing
            # finish_turn — so the FAILED-drill hook there never fires and a
            # drill's RunnerDrill would otherwise strand as pending forever.
            # Mirror that hook here for both sweep outcomes (finding M2): a
            # cancel-requested drill whose runner then disappears is just as
            # stranded as a plain lost one without this.
            # A drill turn is identified by its RunnerDrill FK, not by its origin
            # (drills are ordinary `api` turns that name a runner). The filter
            # below no-ops for a non-drill turn.
            if status in (Turn.LOST, Turn.CANCELLED):
                summary = (
                    "drill turn cancelled (lease expired after cancel was requested)"
                    if status == Turn.CANCELLED
                    else "runner lost the turn (lease expired mid-drill)"
                )
                RunnerDrill.objects.filter(
                    turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
                ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary=summary, finished_at=now)
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


def _refined_allows(r: Runner, t: Turn, defaults: dict, priorities: dict,
                    *, ignore_requirements: bool = False, orders: dict | None = None) -> bool:
    """The per-candidate refinements claim_next_turn applies after the coarse
    target match — same checks, same ORDER, so coverage can't overstate what
    claiming will do.

    `ignore_requirements` answers the counterfactual "would this runner take it
    if the conversation required nothing?" — only ever asked to decide whether
    a requirement is what BLOCKS a turn, never to route one."""
    reqs = frozenset() if ignore_requirements else rr.requirements_of(t)
    if not rr.satisfies(r.flags, reqs):
        return False  # above the pin and the binding, as in claim_next_turn
    # A pin trumps everything below it (claim_next_turn's `pinned_here`).
    if t.pinned_runner_id == r.id:
        return True
    if t.chat_session_id:
        # STICKINESS, mirroring the claim loop's bound_to_me short-circuit: a
        # session bound to this runner claims on it regardless of assignments.
        # Surviving runner_target_q is what IDENTIFIES the holder, so running
        # the assignment check here would report a live chat as `config`
        # ("no runner is assigned") while claiming takes it happily.
        binding = getattr(t.chat_session, "runner_binding", None)
        if binding is not None and binding.runner_id == r.id:
            return True
        routed_agent = t.chat_session.agent_id
    else:
        routed_agent = t.agent_id
    if not routed_agent:
        if _is_repo_turn(t):
            # A repo turn follows its workspace's runner order when it has one:
            # membership only, as for an agent's list (the cascade is about WHEN,
            # not WHETHER — a lower rank still covers the turn).
            rows = repo_order_for(t, orders if orders is not None
                                  else load_workspace_orders({t.workspace_id}), requires=reqs)
            return rows is None or any(row.id == r.id for row in rows)
        return True  # agentless session: runner_target_q had the last word
    # The SAME actor resolution the claim path uses. These two disagreeing is
    # the drift class tests/test_claim_schedule_parity.py exists for: a strict
    # actor rule pointing at an offline box must report `offline`
    # (recoverable), never `config` (never runs).
    rows = assignment_rows_for(
        routed_agent, t.origin, actors.actor_of(t), defaults, priorities, requires=reqs
    )
    return any(row_runner.id == r.id for _rank, row_runner in rows)


def _coverage(ids, runners, defaults: dict, priorities: dict,
              *, ignore_requirements: bool = False) -> dict:
    """{runner: {turn pk it could claim}} over `ids` — the coverage half of both
    `unclaimable_queued_turns` and `turn_reach`, one implementation so the web
    warning and the Slack acknowledgement cannot disagree about the same turn.

    `ignore_requirements=True` is the counterfactual used only to diagnose: which
    runners would take the turn if its conversation required nothing."""
    out: dict = {}
    orders = load_workspace_orders(
        Turn.objects.filter(pk__in=ids, agent__isnull=True, chat_session__isnull=True)
        .values_list("workspace_id", flat=True)
    )
    for r in runners:
        # Same coarse target predicate the claim path uses (assignments +
        # projects + binding-sticky sessions), plus the pin arm — a turn
        # pinned to an offline standby must read "offline", not "config".
        q = runner_target_q(r) | Q(pinned_runner=r)
        covered = set()
        for t in (
            Turn.objects.filter(pk__in=ids).filter(q)
            # A turn pinned ELSEWHERE is invisible to this runner in the claim
            # path; the coarse predicate above does not say so on its own.
            .filter(Q(pinned_runner__isnull=True) | Q(pinned_runner=r))
            .filter(profile_q(r))
            .select_related("agent", "chat_session", "chat_session__runner_binding")
        ):
            # Then the per-source refinement. A runner assigned the agent but
            # excluded by a strict rule for THIS turn's source does not cover
            # it, and saying otherwise would mask a genuinely parked queue.
            if _refined_allows(r, t, defaults, priorities,
                               ignore_requirements=ignore_requirements, orders=orders):
                covered.add(t.pk)
        out[r] = covered
    return out


LIVE, OFFLINE, UNROUTED = "live", "offline", "config"


@dataclass
class Reach:
    """Where one queued turn stands, right now.

    `live` — an ONLINE runner will claim it; `runners` are those runners.
    `offline` — runners could take it, none is online; `runners` are those.
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


def turn_reach(turn: Turn) -> Reach:
    """Will a live runner pick this turn up? Asked the moment it is enqueued, so
    the person who sent it hears "working on it" or "blocked" at once rather than
    inferring it from silence.

    Stricter than `unclaimable_queued_turns` about liveness on purpose: that one
    is an alarm and counts a DEGRADED runner as reachable so a CDP blip does not
    page anyone; this one is a promise, and `claim_next_turn` only claims on
    ONLINE, so a degraded box is not "picking it up".
    """
    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__runner_binding")
            .get(pk=turn.pk))
    if turn.chat_session_id:
        ws = turn.chat_session.workspace_id
    elif turn.agent_id:
        ws = turn.agent.workspace_id
    else:
        ws = turn.workspace_id
    runners = [
        r for r in Runner.objects.exclude(status=Runner.RETIRED).select_related("owner")
        .prefetch_related("declared_flags").order_by("name")
        if ws in runner_tenant_slugs(r)
    ]
    defaults, priorities = _assignment_rows_for_turns([turn])
    covering = [r for r, pks in _coverage({turn.pk}, runners, defaults, priorities).items() if pks]
    live = [r for r in covering if r.live_status == Runner.ONLINE]
    if live:
        return Reach(LIVE, live, behind=blocking_turn(turn))

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

    if covering:
        # Its own runners are merely offline; the requirement is the blocker
        # only if a runner that IS live would otherwise have taken it.
        return Reach(OFFLINE, covering, blocked_by_requirements(live_only=True))
    return Reach(UNROUTED, [], blocked_by_requirements(live_only=False))


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
        .select_related("agent", "chat_session", "chat_session__runner_binding")
        .filter(turn_q if turn_q is not None else Q())
        # A user sees a session turn (and its prompt) only in a chat they may
        # read — the chat ACL, not the tenant. A contact is already narrowed to
        # their own conversations by `turn_q`.
        .filter(_readable_session_turn_q(user))
        .order_by("created_at")
    )
    if not queued:
        return []
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
        r for r in Runner.objects.exclude(status=Runner.RETIRED).select_related("owner")
        .prefetch_related("declared_flags")
        if runner_tenant_slugs(r) & ws_slugs
    ]
    ids = {t.id for t in queued}
    defaults, priorities = _assignment_rows_for_turns(queued)

    reachable = [r for r in runners if r.live_status in (Runner.ONLINE, Runner.DEGRADED)]
    claimable_now = set().union(*_coverage(ids, reachable, defaults, priorities).values())
    # Would ANY paired runner take it if it were up? Separates "misconfigured" from
    # "temporarily unreachable" — the difference between "fix the routing matrix"
    # and "wait, or check the runner".
    claimable_ever = set().union(*_coverage(ids, runners, defaults, priorities).values())
    # Counterfactual, for turns whose conversation requires something and that no
    # runner will ever take: would one take it WITHOUT the requirement? Only then
    # is the requirement the blocker; otherwise the ordinary reason applies.
    required = {t.pk for t in queued if t.pk not in claimable_ever and rr.requirements_of(t)}
    blocked_by_reqs = set().union(*_coverage(required, runners, defaults, priorities,
                                             ignore_requirements=True).values()) if required else set()

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
        if reqs and t.pk in blocked_by_reqs:
            kind = "config"
            reason = (f"this conversation's site requires a {rr.describe(reqs)} runner, and "
                      f"no runner that serves it is declared {rr.describe(reqs)}")
        elif t.capability and t.pk not in claimable_ever:
            kind = "config"
            reason = (f"this is a caller's turn, confined to '{t.capability}', and no runner "
                      "that can confine one serves it — update the runner and the canopy "
                      "plugin on a box assigned to it")
        elif t.pk in claimable_ever:
            kind = "offline"
            reason = f"a runner {what}, but none are reachable right now (offline or heartbeat lapsed)"
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


def claim_next_turn(runner: Runner, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    exclude_slugs: list[str] | None = None) -> Turn | None:
    # Covers the operator PAUSE too: `live_status` returns PAUSED for a parked
    # runner, so this one guard is the whole server-side enforcement. Spelled out
    # because it is easy to read this line as a pure liveness check and then
    # "helpfully" add a pause bypass somewhere below.
    #
    # Note WHERE it sits: above pin matching, so a pause outranks a pin. Both are
    # operator intent, but the pause is the more specific and more recent one, and
    # a pin that could resurrect a parked box would re-open exactly the hole pause
    # closes — work landing on an account that must not spend tokens. The pinned
    # turn stays QUEUED (queued turns never expire) and lands on unpause.
    if runner.live_status != Runner.ONLINE:
        return None
    sweep_expired_leases()
    # Lazy sweeps, both BEFORE the busy_agents read: a turn released here frees
    # its agent for the very claim we are about to make.
    release_stale_occurrence_turns_all()
    skip_late_scheduled_turns()
    projects = runner.project_names()
    session_capable = runner.session_capable()
    has_pins = Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner).exists()
    # A workspace's order counts: agents with no order of their own follow it,
    # so a box named only there still has agent work to look for.
    has_assignments = (
        RunnerAssignment.objects.filter(runner=runner).exists()
        or WorkspaceRunnerOrder.objects.filter(runner=runner, enabled=True).exists()
    )
    if not has_assignments and not projects and not session_capable and not has_pins:
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
                        "chat_session__agent")
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
    # The workspace runner order, for the repo turns among the candidates — the
    # ranking a project dispatch never had (WorkspaceRunnerOrder).
    orders = load_workspace_orders({t.workspace_id for t in candidates if _is_repo_turn(t)})
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
        if turn.agent_id:
            if turn.agent_id not in trusted:
                trusted[turn.agent_id] = runner_may_hold_agent(runner, turn.agent)
            if not trusted[turn.agent_id]:
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

        mode = modes.for_turn(turn, priorities, fresh=True)
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
                )
        except IntegrityError:
            continue  # another runner claimed for this agent between our check and update
        if updated:
            turn.refresh_from_db()
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
