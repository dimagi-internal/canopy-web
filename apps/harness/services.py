"""Harness domain services — the only write path for Runner/Turn/TurnEvent.

Claiming is a single conditional UPDATE (no row can be claimed twice); leases
are renewed by runner heartbeats and swept lazily on claim. All functions are
synchronous and transaction-safe.
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import logging
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from apps.canopy_sessions.staleness import stale_cutoff
from apps.workspaces import services as wsvc

# HEARTBEAT_ONLINE_WINDOW lives on models.py (Runner.live_status uses it too;
# models.py cannot import services.py, which already imports models.py) and is
# re-exported here so existing importers of services.HEARTBEAT_ONLINE_WINDOW keep
# working. Intentional re-export — noqa keeps the F401 gate from deleting it.
from .models import (
    HEARTBEAT_ONLINE_WINDOW,  # noqa: F401
    AgentSchedule,
    Item,
    Runner,
    RunnerAssignment,
    RunnerDrill,
    Turn,
    TurnEvent,
    TurnTranscript,
)

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900


def enqueue_turn(
    *,
    agent=None,
    project: str = "",
    session=None,
    workspace=None,
    origin: str,
    idempotency_key: str,
    prompt: str = "",
    origin_ref: dict | None = None,
    routing: str = Turn.PREFER_LOCAL,
    enqueued_by=None,
    pinned_runner=None,
) -> tuple[Turn, bool]:
    """Queued turns stack freely — the executing-turn index never blocks intake
    (new turns are born `queued`, which the index does not cover).

    Targets exactly one of agent / project / session. A project turn must carry a
    workspace: it has no agent/session to derive tenancy from, and claim_next_turn
    fails it closed without one, so accepting it here would silently queue a turn
    nothing can ever run. Session turns derive tenancy from session.workspace.
    """
    if sum([bool(agent), bool(project), bool(session)]) != 1:
        raise ValueError("a turn targets exactly one of agent / project / session")
    if project and workspace is None:
        raise ValueError("a project turn needs a workspace")
    existing = Turn.objects.filter(idempotency_key=idempotency_key).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            turn = Turn.objects.create(
                agent=agent,
                project=project,
                chat_session=session,
                # Agent + session turns derive tenancy (agent.workspace /
                # chat_session.workspace) and must not denormalize a second copy
                # that can drift; only a project turn carries its own workspace FK.
                workspace=workspace if project else None,
                origin=origin,
                idempotency_key=idempotency_key,
                prompt=prompt,
                origin_ref=origin_ref or {},
                routing=routing,
                enqueued_by=enqueued_by if getattr(enqueued_by, "is_authenticated", False) else None,
                pinned_runner=pinned_runner,
            )
    except IntegrityError:
        # Only possible race: same idempotency key inserted concurrently.
        replay = Turn.objects.filter(idempotency_key=idempotency_key).first()
        if replay is not None:
            return replay, False
        raise
    return turn, True


def heartbeat(
    runner: Runner, *, active_turn_ids: list[str], degraded: bool = False, note: str = "",
    ready: bool = True, ready_note: str = "", code_branch: str = "",
) -> Runner:
    now = timezone.now()
    runner.last_heartbeat_at = now
    runner.status = Runner.DEGRADED if degraded else Runner.ONLINE
    runner.status_note = note
    runner.ready = ready
    runner.ready_note = ready_note
    runner.code_branch = code_branch
    runner.save(update_fields=[
        "last_heartbeat_at", "status", "status_note", "ready", "ready_note", "code_branch",
    ])
    if active_turn_ids:
        Turn.objects.filter(
            pk__in=active_turn_ids,
            claimed_by=runner,
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
        ).update(lease_expires_at=now + dt.timedelta(seconds=DEFAULT_LEASE_SECONDS))
    return runner


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
            if turn.origin == Turn.ORIGIN_DRILL and status in (Turn.LOST, Turn.CANCELLED):
                summary = (
                    "drill turn cancelled (lease expired after cancel was requested)"
                    if status == Turn.CANCELLED
                    else "runner lost the turn (lease expired mid-drill)"
                )
                RunnerDrill.objects.filter(
                    turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
                ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary=summary, finished_at=now)
    return count


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


def _assignment_allows_for_agent(runner: Runner, agent_id, turn: Turn, assignment_map: dict, now) -> bool:
    """assignment_map: {agent_id: [(rank, Runner), ...] ordered by rank}. False when
    this runner is not in the agent's list; True when it is and either every
    better-ranked runner is unavailable or the turn has aged past the grace."""
    rows = assignment_map.get(agent_id) or []
    mine = next((rank for rank, r in rows if r.id == runner.id), None)
    if mine is None:
        return False
    if (now - turn.created_at) >= dt.timedelta(seconds=CASCADE_GRACE_SECONDS):
        return True
    return not any(r.is_available for rank, r in rows if rank < mine)


def _assignment_allows(runner: Runner, turn: Turn, assignment_map: dict, now) -> bool:
    return _assignment_allows_for_agent(runner, turn.agent_id, turn, assignment_map, now)


EXECUTING = [Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]


def runner_target_q(runner: Runner, exclude_slugs: list[str] | None = None) -> Q:
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
    """
    # Both conditions on the SAME assignment row (a single Q, not two ANDed Qs)
    # so they share the join — a disabled row for this runner must not match
    # via some OTHER enabled row on the same agent.
    agent_leg = Q(agent__runner_assignments__runner=runner, agent__runner_assignments__enabled=True)
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
        # assignment cascade; project sessions stay any-sessions-capable).
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
    return q


def runner_tenant_slugs(runner: Runner) -> set[str]:
    """THE tenant a runner may act for — the workspaces of the human who PAIRED
    it, never the Runner.workspace FK.

    One definition, called by every runner-scoped predicate, because this rule
    diverging across call sites is a production outage and not a nicety: claim
    routing once scoped to the FK while `_runner_schedule_qs` derived from
    `paired_by`, so a runner could SEE and FIRE a schedule whose turn it could
    never CLAIM. One laptop runner deliberately serves a fleet spanning
    workspaces, so that stopped 4 of 5 production agents from executing at all
    and their turns sat QUEUED forever (2026-07-25).

    The FK records where a runner LIVES; `paired_by` records who it may work
    FOR. `paired_by` is server-assigned from `request.user` at pairing, so
    unlike the caller-supplied `capabilities` hint it is not attacker-
    controlled. A NULL `paired_by` fails closed (empty set → `__in=set()`
    matches nothing): an orphaned runner has no identity to derive a tenant
    from, and inferring one from the FK would be an escalation.
    """
    return wsvc.user_workspace_slugs(runner.paired_by) if runner.paired_by_id else set()


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


# A turn is not "stuck" the instant it is enqueued — it is queued for a few seconds
# on every normal send while a runner polls (5s) or the WS wake fires. And a runner
# whose heartbeat lapses (>90s) reads STALE, so a flaky laptop network briefly looks
# like "no runners at all". Both made the warning fire on healthy traffic: a phone
# chat send was flagged during a DNS blip, then claimed and answered seconds later.
# Wait longer than a heartbeat window + a claim poll before calling anything stuck.
UNCLAIMABLE_GRACE = dt.timedelta(seconds=150)


def unclaimable_queued_turns(user) -> list[dict]:
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
        .select_related("agent")
        .order_by("created_at")
    )
    if not queued:
        return []
    # Candidate runners for "could ANY runner take this?" are the runners VISIBLE
    # in the caller's tenant, not merely the ones the caller personally paired.
    # Scoping to `paired_by=user` made every stuck turn read as `config` for
    # anyone who didn't pair a runner themselves (a delegated identity, or a
    # teammate in a workspace someone else's runner serves) — the workspace's
    # runner could be sitting right there, offline, and the diagnosis would still
    # say "no runner is assigned; fix your routing." Use the SAME tenancy rule as
    # claim_next_turn (`runner_tenant_slugs`, paired_by-derived, NULL-fails-closed)
    # so this warning can't disagree with what claiming actually does.
    runners = [
        r for r in Runner.objects.exclude(status=Runner.RETIRED).select_related("paired_by")
        if runner_tenant_slugs(r) & ws_slugs
    ]
    ids = {t.id for t in queued}

    def _covered_by(rs) -> set:
        out: set = set()
        for r in rs:
            # Same coarse target predicate the claim path uses (assignments +
            # projects + binding-sticky sessions), plus the pin arm — a turn
            # pinned to an offline standby must read "offline", not "config".
            q = runner_target_q(r) | Q(pinned_runner=r)
            out |= set(Turn.objects.filter(pk__in=ids).filter(q).values_list("pk", flat=True))
        return out

    reachable = [r for r in runners if r.live_status in (Runner.ONLINE, Runner.DEGRADED)]
    claimable_now = _covered_by(reachable)
    # Would ANY paired runner take it if it were up? Separates "misconfigured" from
    # "temporarily unreachable" — the difference between "fix the routing matrix"
    # and "wait, or check the runner".
    claimable_ever = _covered_by(runners)

    out = []
    for t in queued:
        if t.pk in claimable_now:
            continue
        if t.chat_session_id:
            target, what = "session", "can take this session (session-capable + its binding)"
        elif t.agent_id:
            target, what = f"agent {t.agent.slug}", f"is assigned the agent '{t.agent.slug}'"
        else:
            target, what = f"project {t.project}", f"declares the repo '{t.project}'"
        if t.pk in claimable_ever:
            kind = "offline"
            reason = f"a runner {what}, but none are reachable right now (offline or heartbeat lapsed)"
        else:
            kind = "config"
            reason = f"no runner {what}"
        out.append({
            "turn_id": str(t.pk), "target": target, "prompt": (t.prompt or "")[:120],
            "created_at": t.created_at, "reason": reason, "kind": kind,
        })
    return out


def claim_next_turn(runner: Runner, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                    exclude_slugs: list[str] | None = None) -> Turn | None:
    if runner.live_status != Runner.ONLINE:
        return None
    sweep_expired_leases()
    # Lazy sweeps, both BEFORE the busy_agents read: a turn released here frees
    # its agent for the very claim we are about to make.
    release_stale_occurrence_turns_all()
    projects = runner.project_names()
    session_capable = runner.session_capable()
    has_pins = Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner).exists()
    has_assignments = RunnerAssignment.objects.filter(runner=runner).exists()
    if not has_assignments and not projects and not session_capable and not has_pins:
        return None
    routing_q = Q(routing__in=[Turn.PREFER_LOCAL, Turn.LOCAL_ONLY, Turn.ANY])
    if runner.kind == Runner.CLOUD:
        routing_q = Q(routing=Turn.ANY) | Q(routing=Turn.PREFER_LOCAL)
        # prefer_local turns fall to cloud only via the Phase 2 router policy;
        # Phase 0 has no cloud runners, so keep the simple rule: cloud never
        # takes local_only.
    # `agent__isnull=False` is load-bearing for exactly the reason spelled out for
    # busy_sessions below — the same trap, which was fixed there and missed here.
    # A PROJECT turn has agent_id NULL, so without this filter one executing repo
    # turn injected a NULL into this IN-list and every queued AGENT turn evaluated
    # `agent_id IN (…, NULL)` -> NULL -> got wrongly excluded. Not "that agent is
    # busy": the runner claimed NOTHING AT ALL while any project turn ran.
    # Observed on the cloud runner — a drill sat QUEUED and pinned for 40+ minutes
    # while the runner was online and heartbeating and POST /claim returned 204,
    # because two canopy-web project turns happened to be executing.
    busy_agents = Turn.objects.filter(
        status__in=EXECUTING, agent__isnull=False
    ).values("agent_id")
    # A session serializes like an agent: never claim a session that already has
    # an executing turn (one_executing_turn_per_session would reject the claim
    # anyway; this avoids the wasted attempt). The chat_session__isnull=False filter
    # is load-bearing: without it, executing agent/project turns (chat_session_id
    # NULL) would inject a NULL into this IN-list, and every queued SESSION turn
    # would then evaluate `id IN (…, NULL)` -> NULL -> get wrongly excluded whenever
    # any agent turn is running. (Agent/project turns are already protected on the
    # exclude's LEFT side by Django's `AND chat_session_id IS NOT NULL` negation
    # guard — that's a separate mechanism from this filter.)
    busy_sessions = Turn.objects.filter(
        status__in=EXECUTING, chat_session__isnull=False
    ).values("chat_session_id")
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
    # The b4f5ead exploit stays closed: paired_by is server-assigned from
    # request.user at pairing, so unlike capabilities it is not attacker-
    # controlled. An outsider pairing a runner that declares a victim's agent
    # slug gets only THEIR OWN workspaces, so the victim's agent stays
    # unclaimable. Conversely a runner paired by someone who is a member of a
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
        .exclude(agent_id__in=busy_agents)
        .exclude(chat_session_id__in=busy_sessions)
        .filter(tenant_q)
        # _assignment_allows reads turn.agent_id; the session leg's stickiness
        # check reads chat_session.agent_id + chat_session.runner_binding.
        .select_related("agent", "chat_session", "chat_session__runner_binding")
        .order_by("created_at")
    )
    # Two-pass: materialize candidates above, then batch-load every candidate
    # agent's ranked assignment list in one query rather than per-turn. Includes
    # session agents so the cascade check below can look them up too.
    agent_ids = {t.agent_id for t in candidates if t.agent_id} | {
        t.chat_session.agent_id for t in candidates if t.chat_session_id and t.chat_session.agent_id
    }
    assignment_map: dict = {}
    if agent_ids:
        # enabled=True only: a disabled row must neither claim (it's excluded from
        # `rows`, so `mine` in _assignment_allows_for_agent comes back None) nor
        # count as a better-ranked availability blocker for a lower enabled rank.
        rows = (
            RunnerAssignment.objects.filter(agent_id__in=agent_ids, enabled=True)
            .select_related("runner").order_by("rank")
        )
        for row in rows:
            assignment_map.setdefault(row.agent_id, []).append((row.rank, row.runner))
    now = timezone.now()
    for turn in candidates:
        pinned_here = turn.pinned_runner_id == runner.id
        if not pinned_here:
            if not _kind_allows(runner, turn.routing):
                continue
            if turn.agent_id:
                if not _assignment_allows(runner, turn, assignment_map, now):
                    continue
            if turn.chat_session_id:
                sess = turn.chat_session
                binding = getattr(sess, "runner_binding", None)
                bound_to_me = binding is not None and binding.runner_id == runner.id
                if not bound_to_me and sess.agent_id:
                    if not _assignment_allows_for_agent(runner, sess.agent_id, turn, assignment_map, now):
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
                )
        except IntegrityError:
            continue  # another runner claimed for this agent between our check and update
        if updated:
            turn.refresh_from_db()
            append_events(turn, [{"kind": "status", "payload": {"status": Turn.CLAIMED, "runner": runner.name}}])
            return turn
    return None


def append_events(turn: Turn, events: list[dict]) -> int:
    with transaction.atomic():
        # Lock the turn row first so concurrent appenders to the same turn
        # serialize on the Max("seq") read instead of racing each other into
        # the turnevent_seq_unique_per_turn index (sqlite ignores
        # select_for_update; Postgres serializes — that's the point).
        Turn.objects.select_for_update().get(pk=turn.pk)
        current = (
            TurnEvent.objects.filter(turn=turn).aggregate(m=Max("seq"))["m"] or 0
        )
        rows = [
            TurnEvent(turn=turn, seq=current + i + 1, kind=e["kind"], payload=e.get("payload", {}))
            for i, e in enumerate(events)
        ]
        TurnEvent.objects.bulk_create(rows)

    # Fire AFTER commit so subscribers (apps/realtime) fan out durable rows and
    # never race the DB. Local import + on_commit avoids an import-time cycle and
    # a fan-out on a transaction that ultimately rolls back. bulk_create emits no
    # post_save, so this signal is the only hook a live tail can ride.
    def _fire_appended():
        from apps.harness.signals import turn_events_appended

        turn_events_appended.send(sender=Turn, turn=turn, rows=rows)

    transaction.on_commit(_fire_appended)
    return len(rows)


# Per-TURN ceiling on retained raw transcript content (security review
# 2026-07-26, F2). `raw_jsonl_gz` is Postgres `bytea` (1GB hard limit) and
# `bytes_raw` a `PositiveIntegerField` (2GB) — an unbounded single turn would
# eventually hit one of those and raise a raw DB error mid-turn with no
# upstream signal. 100MB (uncompressed) is generous headroom above even an
# unusually long, tool-output-heavy turn while sitting multiple orders of
# magnitude under both hard limits, so this is a backstop against a runaway
# turn, not a realistic ceiling for normal use.
TRANSCRIPT_TURN_MAX_BYTES = 100 * 1024 * 1024


def append_transcript(turn: Turn, raw_lines: list[str], *, batch_id: str = "") -> TurnTranscript:
    """Accumulate raw `claude -p` JSONL lines onto a turn's retained transcript.

    Idempotent-per-turn in the sense that repeated calls ACCUMULATE (a turn
    streams in batches over its lifetime) — never replace. Lines are joined
    with a bare "\\n" exactly as the CLI's own JSONL framing does; no
    re-encoding, no reordering, no rewriting a line's content. canopy stores
    bytes only and never parses this JSONL — that stays the consumer's job.

    O(1) per append: rather than decompress-everything-then-recompress-
    everything (O(total accumulated) on every call — expensive while holding
    the same Turn row lock the claim/finish paths take), this gzip-compresses
    only THIS batch and concatenates the resulting gzip member onto the
    stored blob. `gzip.decompress` transparently reassembles a concatenated
    multi-member stream (stdlib-verified:
    `gzip.decompress(gzip.compress(b"a") + gzip.compress(b"b")) == b"ab"`),
    so this is backward compatible with rows already written as a single
    member — no migration, no format break.

    A caller that splits a stream chunk on "\\n" will periodically produce a
    batch whose only element is the trailing empty segment — a real but
    zero-byte "line". Those are dropped before counting/encoding so
    `line_count` never claims a line the stored bytes don't have; an
    all-blank batch is a true no-op (existing content, counters, and stored
    bytes are all left untouched).

    An element that itself contains an embedded "\\n" violates the one-
    JSONL-record-per-element contract (it understates `line_count` and would
    inject a stray join at the next append) — logged as a warning so a
    Task-2 upstream bug surfaces at the boundary instead of as an unexplained
    later cost discrepancy. Not raised: a malformed batch should still be
    retained, not dropped.

    `batch_id` (security review F5) is an optional caller-supplied idempotency
    key for THIS batch. If it matches the turn's `last_batch_id` — the
    immediately preceding call — this is a retry after a lost response, and
    the batch is dropped as a no-op rather than double-appended (a lost-ack
    retry is the realistic case; an arbitrary OLDER batch replayed later is
    not guarded against). Omit it (empty string, the default) to skip
    dedup entirely — existing/older callers are unaffected.

    Per-turn size ceiling (F2): once accumulated `bytes_raw` would cross
    `TRANSCRIPT_TURN_MAX_BYTES`, this batch's actual content is DROPPED and a
    single synthetic marker line is written in its place, then `truncated`
    latches permanently — every later call for this turn is a silent no-op.
    A turn still executing must not be failed over transcript SIZE, so this
    never raises; the caller (the HTTP route) always sees success.
    """
    # Drop truly-empty elements (see docstring) before both the count and the
    # join — a splitter's trailing "" must never count as a stored line.
    lines = [line for line in raw_lines if line != ""]

    if any("\n" in line for line in lines):
        logger.warning(
            "append_transcript(turn=%s): a raw line contains an embedded "
            "newline, violating the one-JSONL-record-per-element contract — "
            "line_count and the stored join structure will be wrong for this "
            "batch",
            turn.pk,
        )

    with transaction.atomic():
        # Lock the turn row first so concurrent appenders to the same turn
        # serialize (mirrors append_events — sqlite ignores select_for_update,
        # Postgres serializes, which is the point).
        Turn.objects.select_for_update().get(pk=turn.pk)
        transcript = (
            TurnTranscript.objects.select_for_update().filter(turn=turn).first()
        )

        if batch_id and transcript is not None and transcript.last_batch_id == batch_id:
            # A retry of the batch we JUST applied (its response was lost in
            # transit) — already reflected in the stored content, so this is
            # a no-op, not a double-append.
            return transcript

        if transcript is not None and transcript.truncated:
            # Per-turn ceiling already hit — drop everything further,
            # including a marker (that was written exactly once, at the
            # crossing call below).
            return transcript

        content = "\n".join(lines)
        added_lines = len(lines)
        existing_bytes = transcript.bytes_raw if transcript is not None else 0
        newly_truncated = False
        if existing_bytes + len(content.encode("utf-8")) > TRANSCRIPT_TURN_MAX_BYTES:
            # This batch would cross the ceiling. Drop its actual content —
            # never mind what it was — and write ONE synthetic marker line
            # instead, so a re-derivation downstream can see the transcript
            # was cut off rather than silently ending mid-stream.
            content = json.dumps({
                "type": "canopy_transcript_truncated",
                "reason": (
                    f"turn transcript exceeded {TRANSCRIPT_TURN_MAX_BYTES} "
                    "bytes; further content for this turn was dropped"
                ),
            })
            added_lines = 1
            newly_truncated = True

        # A bare "\n" glues this content onto whatever's already stored — but
        # only when both sides are non-empty, so a first-ever or all-blank
        # batch never introduces a phantom separator.
        if transcript is not None and transcript.bytes_raw and content:
            new_raw = ("\n" + content).encode("utf-8")
        else:
            new_raw = content.encode("utf-8")

        added_bytes = len(new_raw)
        new_member = gzip.compress(new_raw) if new_raw else b""

        if transcript is None:
            transcript = TurnTranscript.objects.create(
                turn=turn,
                raw_jsonl_gz=new_member,
                line_count=added_lines,
                bytes_raw=added_bytes,
                truncated=newly_truncated,
                last_batch_id=batch_id,
            )
        elif new_member:
            transcript.raw_jsonl_gz = bytes(transcript.raw_jsonl_gz) + new_member
            transcript.line_count = transcript.line_count + added_lines
            transcript.bytes_raw = transcript.bytes_raw + added_bytes
            if newly_truncated:
                transcript.truncated = True
            if batch_id:
                transcript.last_batch_id = batch_id
            transcript.save(
                update_fields=[
                    "raw_jsonl_gz", "line_count", "bytes_raw", "truncated",
                    "last_batch_id", "updated_at",
                ]
            )
        # else: an all-blank batch on top of existing content — nothing new
        # to add, leave the row untouched (batch_id is deliberately not
        # recorded here either: replaying a genuinely blank batch is already
        # a no-op, so there's nothing dedup needs to protect).
        return transcript


def read_transcript(turn: Turn) -> bytes:
    """Decompressed raw JSONL for a turn, or b"" if nothing was ever appended
    (a turn with no transcript is common — e.g. non-CLI turns — and must read
    as empty rather than raise).

    For in-process consumers only (e.g. a future cost-derivation job running
    server-side, or anything that genuinely needs the whole blob at once).
    The HTTP read route does NOT call this — see `iter_transcript`, which
    streams bounded chunks instead of materializing the whole decompressed
    blob in a web worker's memory (security review 2026-07-26, F3)."""
    transcript = TurnTranscript.objects.filter(turn=turn).first()
    if transcript is None or not transcript.raw_jsonl_gz:
        return b""
    return gzip.decompress(bytes(transcript.raw_jsonl_gz))


def iter_transcript(turn: Turn, *, chunk_size: int = 64 * 1024):
    """Yield a turn's DECOMPRESSED raw JSONL in bounded chunks, inflating
    incrementally rather than materializing the whole decompressed blob at
    once (security review 2026-07-26, F3; the sibling `/events` route caps
    at 500 rows for the same underlying reason — nothing about this route
    may scale with transcript size).

    An EARLIER version of this fix instead served the STILL-GZIPPED bytes
    directly with `Content-Encoding: gzip`, betting on the HTTP client to
    inflate transparently. A follow-up review empirically falsified that:
    `curl --compressed` and `httpx` both return only the FIRST gzip member
    of a multi-member stream (Task 1's own on-disk format — see
    `append_transcript`) — a 200 with silently TRUNCATED content, no error,
    exactly the corrupted-derivation failure mode F5's idempotency work
    exists to prevent. Worse, this repo's own runner client
    (`packages/canopy_runner`, `urllib.request`) sends no `Accept-Encoding`
    and does no decoding at all — it would treat raw gzip bytes as JSONL.
    Streaming plaintext removes the wire-format gamble entirely: every
    caller sees the same bytes `read_transcript` would return, with none of
    read_transcript's all-at-once memory cost.

    `gzip.GzipFile` transparently reassembles Task 1's concatenated
    multi-member blob exactly as `gzip.decompress` does — this is just the
    same decompression, read incrementally instead of all at once. Yields
    nothing (an empty generator) when the turn has no transcript."""
    transcript = TurnTranscript.objects.filter(turn=turn).first()
    if transcript is None or not transcript.raw_jsonl_gz:
        return
    with gzip.GzipFile(fileobj=io.BytesIO(bytes(transcript.raw_jsonl_gz))) as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            yield chunk


def mark_running(turn: Turn, *, session_id: str = "") -> Turn:
    """Transition CLAIMED|RUNNING -> RUNNING. A no-op (no event, no field
    writes) if the turn was swept to a terminal state (e.g. lost) underneath
    the caller — guards against a zombie runner resurrecting a dead turn."""
    now = timezone.now()
    fields: dict = {"status": Turn.RUNNING}
    if not turn.started_at:
        fields["started_at"] = now
    if session_id:
        fields["session_id"] = session_id
    updated = Turn.objects.filter(
        pk=turn.pk, status__in=[Turn.CLAIMED, Turn.RUNNING]
    ).update(**fields)
    turn.refresh_from_db()
    if not updated:
        return turn
    append_events(turn, [{"kind": "status", "payload": {"status": Turn.RUNNING}}])
    return turn


def finish_turn(
    turn: Turn, *, status: str, result_note: str = "", allow_queued: bool = False
) -> Turn:
    """Transition CLAIMED|RUNNING|NEEDS_HUMAN -> DONE|FAILED|MISSED|CANCELLED. A
    no-op (no event, no field writes) if the turn is already terminal —
    idempotent, and guards against resurrecting a turn already swept to lost.

    A QUEUED turn is deliberately NOT finishable by default: a runner must never
    finish a turn it never claimed (the API surfaces that attempt as a 409).
    `allow_queued=True` is the scheduler's opt-in — it is a different actor, and
    a slot nobody ever picked up is the textbook MISSED. Without it, supersede
    would silently skip queued occurrences and the board would accumulate them.
    """
    if status not in (Turn.DONE, Turn.FAILED, Turn.MISSED, Turn.CANCELLED):
        raise ValueError(f"finish status must be done|failed|missed|cancelled, got {status!r}")
    if status == Turn.DONE and turn.events.filter(kind="cancel_requested").exists():
        # Server-side backstop (a deaf/poll-only runner can miss the
        # runner.cancel control frame entirely and finish DONE anyway,
        # stranding a cancel_requested event with no CANCELLED turn to match
        # it). Mirrors sweep_expired_leases's same reasoning: the user asked to
        # stop, so a full reply that raced through the interrupt is still a
        # cancelled turn, not a done one.
        status = Turn.CANCELLED
    now = timezone.now()
    from_states = [Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]
    if allow_queued:
        from_states.append(Turn.QUEUED)
    updated = Turn.objects.filter(pk=turn.pk, status__in=from_states).update(
        status=status, finished_at=now, result_note=result_note
    )
    turn.refresh_from_db()
    if not updated:
        return turn
    append_events(turn, [{"kind": "status", "payload": {"status": status, "result_note": result_note}}])
    # A finished scheduled occurrence discharges any open nag for its schedule —
    # you no longer owe attention to a slot that has since completed.
    if status == Turn.DONE:
        sid = (turn.origin_ref or {}).get("schedule_id")
        if sid:
            resolve_schedule_nags(sid)
    # A drill turn that fails outright (auth expired, environment broken) resolves
    # its RunnerDrill without waiting for the agent's own report callback — the
    # agent may never get far enough to curl the callback at all. Scoped to
    # OUTCOME_PENDING so a drill already resolved by a report is not clobbered.
    # CANCELLED is included too: drills queue behind real executing turns
    # (start_drill's docstring) and the plain /turns/{id}/cancel route has no
    # origin filter, so a queued drill can be cancelled out from under itself —
    # without this it would strand OUTCOME_PENDING forever, the same failure
    # mode this hook exists to prevent for FAILED.
    if turn.origin == Turn.ORIGIN_DRILL and status in (Turn.FAILED, Turn.CANCELLED):
        if status == Turn.CANCELLED:
            summary = "drill turn cancelled"
        else:
            summary = result_note or "drill turn failed"
        RunnerDrill.objects.filter(
            turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
        ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary=summary, finished_at=now)
    return turn


def cancel_queued_turn(turn: Turn) -> Turn | None:
    """Best-effort un-queue: mark a still-QUEUED turn CANCELLED. Cancel is
    'un-queue', not 'kill' — a CLAIMED/RUNNING turn is owned by its runner's
    lease and is left alone (returns None). Terminal turns also return None
    (idempotent no-op). The REST cancel view and chat's `chat.stop` both route
    through here."""
    if turn.status != Turn.QUEUED:
        return None
    return finish_turn(turn, status=Turn.CANCELLED, result_note="cancelled", allow_queued=True)


def cancel_turn(turn: Turn) -> Turn | None:
    """Full cancel semantics for chat.stop / the REST stop route. A QUEUED turn
    is finished CANCELLED immediately. An executing turn is NOT force-finished —
    the runner owns its lease — instead we record cancel_requested in the ledger
    and signal the claiming runner over its control channel; the runner interrupts
    the emdash session and finishes the turn as cancelled (or, if the runner is
    gone, the lease sweep sees cancel_requested and closes it CANCELLED)."""
    if turn.status == Turn.QUEUED:
        # Race guard (finding M1): this is a read-then-act on `turn.status`, and
        # a runner's claim can land between the read and the write, moving the
        # turn QUEUED -> CLAIMED underneath us. Routing that through
        # finish_turn(allow_queued=True) would be wrong here — its from_states
        # already includes CLAIMED/RUNNING/NEEDS_HUMAN, so it would happily
        # force-finish the now-claimed turn as CANCELLED out from under the
        # runner that just picked it up. Guard the UPDATE itself on
        # status=QUEUED so only a turn still queued at write-time is affected.
        now = timezone.now()
        updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
            status=Turn.CANCELLED, finished_at=now, result_note="cancelled",
        )
        turn.refresh_from_db()
        if updated:
            append_events(turn, [{
                "kind": "status",
                "payload": {"status": Turn.CANCELLED, "result_note": "cancelled"},
            }])
            # Mirror finish_turn's drill hook (bypassed above) so a queued
            # drill cancelled this way doesn't strand OUTCOME_PENDING.
            if turn.origin == Turn.ORIGIN_DRILL:
                RunnerDrill.objects.filter(
                    turn=turn, outcome=RunnerDrill.OUTCOME_PENDING
                ).update(outcome=RunnerDrill.OUTCOME_FAIL, summary="drill turn cancelled", finished_at=now)
            return turn
        # Lost the race: the turn is no longer QUEUED (claimed out from under
        # us, or already terminal). Fall through to the freshly-refreshed
        # status below rather than the stale one we started with.
    if turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN):
        append_events(turn, [{"kind": "cancel_requested", "payload": {}}])
        if turn.claimed_by_id:
            from apps.realtime import groups

            groups.publish(groups.runner_group(turn.claimed_by_id),
                           {"type": "runner.cancel", "turn_id": str(turn.id)})
        return turn
    return None


# --------------------------------------------------------------------------------------
# AgentSchedule — recurring turns. The runner evaluates the cron and calls fire_schedule;
# the server materializes a normal Turn. See models.AgentSchedule.
# --------------------------------------------------------------------------------------

def _occurrences(schedule):
    """This schedule's turns — scheduled AND manual. Occurrence-based, not
    origin-based: a "Run now" turn is an attempt at the same work, so it must
    participate in latest/supersede/release exactly as a fired slot does."""
    return Turn.objects.filter(
        agent_id=schedule.agent_id, origin_ref__schedule_id=schedule.id
    )


def latest_occurrence_turn(schedule) -> Turn | None:
    """The newest turn this schedule produced — scheduled or manual run-now —
    whatever its status."""
    return _occurrences(schedule).order_by("-created_at").first()


def supersede_open_turns(schedule, *, reason: str) -> int:
    """Terminate this schedule's non-terminal turns as MISSED. Supersede and
    grace-release are the same operation at two timescales."""
    open_turns = _occurrences(schedule).filter(status__in=list(Turn.NON_TERMINAL))
    count = 0
    for turn in open_turns:
        finish_turn(turn, status=Turn.MISSED, result_note=reason, allow_queued=True)
        count += 1
    return count


def fire_schedule(schedule, slot: dt.datetime) -> tuple[Turn, bool]:
    """Materialize `slot` as a queued Turn. Supersedes any still-open occurrence
    of the same schedule first — you only ever owe the newest.

    Safe to call concurrently from both macOS-account runners: the slot-derived
    idempotency_key collapses the race inside enqueue_turn.
    """
    key = f"sched:{schedule.id}:{slot.isoformat()}"
    with transaction.atomic():
        if not Turn.objects.filter(idempotency_key=key).exists():
            supersede_open_turns(schedule, reason=f"superseded by slot {slot.isoformat()}")
        turn, created = enqueue_turn(
            agent=schedule.agent,
            origin=Turn.ORIGIN_CRON,
            idempotency_key=key,
            prompt=schedule.prompt,
            origin_ref={"schedule_id": schedule.id, "slot": slot.isoformat()},
            routing=schedule.routing,
        )
        if created and (schedule.last_slot is None or slot > schedule.last_slot):
            schedule.last_slot = slot
            schedule.save(update_fields=["last_slot", "updated_at"])
    return turn, created


def run_schedule_now(schedule) -> Turn:
    """Manual off-cycle trigger. Supersedes any still-open occurrence first,
    exactly as fire_schedule does — you only ever owe the newest, however it was
    launched. Run now is the designed remediation for an unfinished slot, so it
    must retire the slot it remediates; otherwise finishing the manual turn
    clears the nag (it is the newest occurrence) while the slot turn sits queued
    and still owed, and the work runs a second time when it is claimed later.

    origin=manual with a uuid-suffixed key, so an ad-hoc run never collides with
    a real slot, and last_slot is untouched — the CADENCE is unaffected (the next
    real slot still fires on time).
    """
    with transaction.atomic():
        supersede_open_turns(schedule, reason="superseded by a manual run")
        turn, _ = enqueue_turn(
            agent=schedule.agent,
            origin=Turn.ORIGIN_MANUAL,
            idempotency_key=f"sched:{schedule.id}:manual:{uuid.uuid4()}",
            prompt=schedule.prompt,
            origin_ref={"schedule_id": schedule.id, "manual": True},
            routing=schedule.routing,
        )
    return turn


def release_stale_occurrence_turns(schedule, *, now: dt.datetime | None = None) -> int:
    """Release this schedule's turns that have HELD the agent past grace_minutes.

    This is what keeps a forgotten session from wedging the agent: an executing
    turn holds one_executing_turn_per_agent, and the runner's heartbeat keeps
    renewing its lease for as long as the emdash session is open, so the ordinary
    lease sweep never rescues it.

    Scoped to EXECUTING (not NON_TERMINAL) and anchored on claimed_at (not
    created_at) because both are statements about *holding*, which is what
    grace_minutes bounds:
      - a QUEUED turn holds nothing (the index does not cover it), so releasing
        it could not unwedge anything — it would only destroy work still owed
        (laptop offline over a weekend must not retire Friday's slot). Retiring
        a stale queued occurrence is supersede_open_turns' job, at the right
        moment.
      - created_at measures *owed* time, so a turn queued longer than grace would
        be born past-grace and get aborted on its first sweep after being claimed
        — killing live human work in the function meant to protect it.
    claimed_at is non-null for every EXECUTING turn: claim_next_turn writes it,
    and claiming is the only route into those states.
    """
    now = now or timezone.now()
    cutoff = now - dt.timedelta(minutes=schedule.grace_minutes)
    stale = _occurrences(schedule).filter(status__in=EXECUTING, claimed_at__lt=cutoff)
    count = 0
    for turn in stale:
        finish_turn(
            turn, status=Turn.MISSED,
            result_note=f"released after {schedule.grace_minutes}m unattended",
        )
        _raise_schedule_nag(schedule, turn)
        count += 1
    return count


def release_stale_occurrence_turns_all(*, now: dt.datetime | None = None) -> int:
    """Fleet-wide release, run lazily on the claim tick (see claim_next_turn).

    Release belongs here, not on the fire tick: fire already supersedes
    everything release would touch, and a weekly schedule's fire tick is 10,080
    minutes apart — it could never honour a 120-minute grace between
    occurrences. On claim, a release unblocks the very same claim.

    The scan is a handful of rows: the executing-turn index caps this at ~one
    turn per agent.
    """
    now = now or timezone.now()
    schedule_ids = {
        turn.origin_ref.get("schedule_id")
        for turn in Turn.objects.filter(status__in=EXECUTING).only("origin_ref")
    }
    schedule_ids.discard(None)
    if not schedule_ids:
        return 0
    return sum(
        release_stale_occurrence_turns(schedule, now=now)
        for schedule in AgentSchedule.objects.filter(id__in=schedule_ids)
    )


# --------------------------------------------------------------------------------------
# RunnerBinding reuse — durable thread↔session mapping (cross-account); see
# apps.canopy_sessions.models.RunnerBinding
# --------------------------------------------------------------------------------------

def _binding_for_thread(agent, project, workspace, thread_key):
    """The RunnerBinding for a (target, thread_key), or None. Enforces the
    agent-XOR-project rule the way _link_target used to: an agent thread matches on
    session.agent and ignores workspace (derived via the agent); a project thread
    matches on session.project AND session.workspace (its identity, so a guessed
    thread_key from another tenant lands on its own row, never the victim's)."""
    from apps.canopy_sessions.models import RunnerBinding

    if bool(agent) == bool(project):
        raise ValueError("a session reuse lookup targets an agent XOR a project")
    qs = RunnerBinding.objects.select_related("session", "runner").filter(thread_key=thread_key)
    if agent:
        return qs.filter(session__agent=agent).first()
    if workspace is None:
        raise ValueError("a project session reuse lookup needs a workspace")
    return qs.filter(
        session__agent__isnull=True, session__project=project, session__workspace=workspace
    ).first()


def _thread_session(agent, project, workspace, thread_key):
    """Find-or-create the durable Session a thread maps to. A chat thread_key is
    str(session.id) — bind that exact existing Session. Otherwise create a durable
    origin=runner Session for the phone/agent/project thread.

    Session.workspace is required (not nullable) but Agent.workspace IS nullable
    ("migration safety" per apps/agents/models.py) — an agent thread with no
    workspace of its own falls back to the tenancy default, mirroring every other
    app's `wsvc.ensure_default_workspace()` fallback (apps/projects/api.py et al),
    rather than crashing the record-session call with a NOT NULL violation."""
    from apps.canopy_sessions.models import Session

    try:
        existing = Session.objects.filter(pk=uuid.UUID(str(thread_key))).first()
    except (ValueError, TypeError):
        existing = None
    if existing is not None:
        return existing
    return Session.objects.create(
        agent=agent,
        project=project or "",
        workspace=workspace or (agent.workspace if agent else None) or wsvc.ensure_default_workspace(),
        origin=Session.ORIGIN_RUNNER,
        title=thread_key[:200],
    )


def resolve_session(agent, thread_key: str, runner: Runner, *, project: str = "", workspace=None) -> dict:
    """Given (target, thread_key) and the CURRENTLY-active runner, decide how to execute.

    `agent` may be None when `project` is given — the phone addresses repos too.

    Returns a plan dict:
      - reuse (bool): the live session hint is owned by THIS runner/host — the runner
        should verify the emdash task still exists and drive it (send prompt into it).
      - emdash_task_id: the task to reuse (only meaningful when reuse=True).
      - agent_task_ext_id / summary: durable context for rehydration when reuse=False
        (fresh session under this account) or for a brand-new thread.
      - link_id: the RunnerBinding's session id (None if no binding exists yet — brand-new
        thread).

    Never assumes the live session is reachable: reuse is only proposed when the hint's
    runner + macOS host match the caller (the two-account failover invariant)."""
    binding = _binding_for_thread(agent, project, workspace, thread_key)
    if binding is None:
        return {"reuse": False, "emdash_task_id": "", "agent_task_ext_id": "",
                "summary": "", "link_id": None, "new_thread": True}
    return {
        "reuse": binding.reusable_by(runner),
        "emdash_task_id": binding.session_key,
        "agent_task_ext_id": binding.agent_task_ext_id,
        "summary": binding.summary,
        "link_id": str(binding.session_id),
        "new_thread": False,
    }


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce a possibly-naive datetime to aware (UTC). The runner sends ISO8601
    (typically already UTC via a trailing "Z"), but a naive value would otherwise
    hit Django's USE_TZ=True as a silent local-time footgun rather than a clean
    UTC stamp."""
    if value is None:
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, dt.UTC)
    return value


def record_session(
    agent,
    thread_key: str,
    *,
    runner: Runner,
    project: str = "",
    workspace=None,
    emdash_task_id: str = "",
    session_id: str = "",  # accepted for wire-compat; the binding keys on session_key
    agent_task_ext_id: str | None = None,
    summary: str | None = None,
):
    """Upsert the thread's durable Session + RunnerBinding and re-point the live-session
    hint at THIS runner/host. Only overwrites agent_task_ext_id/summary when passed,
    preserving accumulated context. The API caller has already gated the runner's
    pairer against `workspace` — this stores, it does not authorize."""
    from apps.canopy_sessions.models import RunnerBinding

    with transaction.atomic():
        binding = _binding_for_thread(agent, project, workspace, thread_key)
        if binding is None:
            session = _thread_session(agent, project, workspace, thread_key)
            binding = (
                RunnerBinding.objects.select_for_update()
                .filter(session=session)
                .first()
            )
            if binding is None:
                binding = RunnerBinding(session=session)
        binding.thread_key = thread_key
        binding.runner = runner
        binding.host = runner.host
        binding.session_key = emdash_task_id
        # Name the row after the emdash task the human actually sees.
        # _thread_session titles a BRAND-NEW session with the raw thread_key,
        # which for an agent turn is an opaque hash (a real one leaked into the
        # Sessions list as "19f91250349ec91b"). Only retitle when the title is
        # still that fallback — never clobber a human-set chat title.
        # Retitle when the current title is NOT a name the human chose. Two
        # cases qualify: the raw thread_key fallback (an opaque hash for an agent
        # turn — one leaked into the Sessions list as "19f91250349ec91b"), and an
        # AUTOTITLE, which is the first user message truncated to 80 chars.
        #
        # The autotitle case is why this is more than an equality check: a
        # phone-created session gets autotitled before any emdash task exists, so
        # the old `title == thread_key` guard never matched and the session kept a
        # sentence for a name while the sidebar showed the task
        # (observed 2026-07-27). A human-set title is still never clobbered — it
        # won't match the fallback and won't match the first message either.
        if emdash_task_id and _title_is_derived(binding.session, thread_key):
            binding.session.title = emdash_task_id[:200]
            binding.session.save(update_fields=["title"])
        binding.live_seen_at = timezone.now()
        if agent_task_ext_id is not None:
            binding.agent_task_ext_id = agent_task_ext_id
        if summary is not None:
            binding.summary = summary
        binding.save()
    return binding


@transaction.atomic

def _title_is_derived(session, thread_key: str) -> bool:
    """True when a session's title was generated rather than chosen by a human.

    Generated titles are safe to replace with the emdash task name; a title
    somebody typed is not. Two generators exist: `_thread_session`'s raw
    thread_key fallback, and `autotitle.maybe_autotitle`, which takes the first
    user message, collapses whitespace and truncates to TITLE_MAX.
    """
    from apps.canopy_sessions.autotitle import TITLE_MAX
    from apps.canopy_sessions.models import Message

    title = (session.title or "").strip()
    if not title or title == thread_key:
        return True
    first = (
        Message.objects.filter(session=session, role=Message.USER)
        .order_by("turn_index")
        .values_list("plaintext", flat=True)
        .first()
    )
    if not first:
        return False
    return title == " ".join(first.split())[:TITLE_MAX]


def replace_reported_sessions(
    runner: Runner, workspace, sessions: list, archived: list[str] | None = None
) -> int:
    """Upsert a durable Session(origin=runner) + RunnerBinding per reported
    session. Sessions that fell off the report keep their Session row but have
    their live binding cleared.

    `archived` is the CLOSING signal — emdash task names this runner has seen
    archived. Absence from `sessions` is ambiguous (archived? runner down?
    truncated?), so it can never retire a row on its own; an explicit name here can.
    Scoped to THIS runner's bindings, because a task name is not unique across
    machines and one laptop must never retire another's session.

    The SAME binding this writes doubles as the reuse target for a phone-
    dispatched "Continue" turn (`origin_ref.thread_key = "emdash:<task>"`,
    e.g. `OpenSessions.tsx`) — `thread_key` + `host` are stamped ONLY when the
    binding is freshly created here, so `_binding_for_thread` can find a
    project-Continue row that has no other origin. Pre-fold (SessionLink era)
    a SECOND row existed purely for that lookup; now there is only one row,
    but an existing binding's durable identity (thread_key/host) is left
    untouched on update — the runner's ambient sweep reports EVERY open
    emdash task (agent- or project-driven, no filter), so a session already
    bound by `record_session` to an agent/phone thread must not have that
    binding's thread_key silently reassigned to `emdash:<task>` underneath it
    (that would orphan the agent thread's reuse lookup and fork a duplicate
    session on its next turn)."""
    from apps.canopy_sessions.models import RunnerBinding, Session

    # emdash task NAMES are not unique — two un-archived tasks can share a name
    # (see task_state's "Names aren't unique in emdash's schema" note). Collapse
    # duplicates before upserting; the runner sends newest-first, so the first
    # occurrence is the live session and an older namesake is stale and correctly
    # dropped (observed 2026-07-20 with two "mobile" tasks).
    deduped, seen = [], set()
    for s in sessions:
        if s.emdash_task in seen:
            continue
        seen.add(s.emdash_task)
        deduped.append(s)

    now_keys = {s.emdash_task for s in deduped}

    # The loop takes select_for_update locks, which Django REJECTS outside a
    # transaction — "select_for_update cannot be used outside of a transaction".
    # That was latent from #350: nothing in the loop wrote after taking the lock,
    # so the error was never raised. Adding a `.save()` (the title repair) made it
    # fire, 500ing the endpoint the runner calls every ~10s to report which
    # sessions are alive — the whole machine went dark on liveness.
    #
    # Wrapping the loop is the real fix: the lock was always meant to serialize
    # concurrent reports for a task, and without a transaction it never did.
    with transaction.atomic():
        for s in deduped:
            # Find this runner's binding for the task WITHOUT depending on the live
            # `runner` FK — the clear step below nulls it for anything that fell off the
            # report, and a lookup keyed on it would then miss the row and fork a
            # DUPLICATE Session when the task reappears. The two branches are
            # asymmetric on purpose: `runner=runner` preserves today's behaviour
            # exactly while the FK is set (legacy bindings carry host="" and would stop
            # matching if we keyed on host alone), and the null branch recovers a row
            # THIS runner previously released — scoped by host, because emdash task
            # names collide across machines and one laptop must never claim another's.
            # The null branch requires a NON-BLANK host: a legacy binding with host=""
            # would otherwise be recoverable by any runner whose own host is "" (two
            # un-heartbeated runners would fuse). `runner=runner` still covers a
            # host="" binding this runner currently owns, so that case is unaffected.
            host_match = Q(runner__isnull=True) & Q(host=runner.host) & ~Q(host="")
            binding = (
                RunnerBinding.objects.select_for_update()
                .filter(session_key=s.emdash_task)
                .filter(Q(runner=runner) | host_match)
                .first()
            )
            if binding is None:
                session = Session.objects.create(
                    workspace=workspace,
                    origin=Session.ORIGIN_RUNNER,
                    project=s.project or "",
                    title=s.emdash_task,
                )
                binding = RunnerBinding(session=session, session_key=s.emdash_task)
                binding.thread_key = f"emdash:{s.emdash_task}"
                binding.host = runner.host
            else:
                # Correct a GENERATED title on an existing session. A brand-new
                # session above is titled from the emdash task, but an existing one
                # never was — so a session created on the phone kept its autotitle
                # (the first user message, truncated) forever, while emdash's own
                # sidebar showed the task name. The first fix went into
                # `record_session`, which only runs when a TURN is routed; this is
                # the path that runs every ~10s, which is why the repair never
                # actually happened (observed 2026-07-27, after shipping it).
                #
                # `_title_is_derived` recognises only titles WE generated, so a title
                # a human chose is still never touched.
                # Cosmetic repair, kept isolated: a title must never cost liveness.
                # The enclosing `transaction.atomic()` (see below) is what makes the
                # select_for_update above legal at all; this inner block only stops a
                # retitle failure from rolling the whole report back.
                try:
                    with transaction.atomic():
                        if _title_is_derived(binding.session, binding.thread_key or ""):
                            if binding.session.title != s.emdash_task:
                                binding.session.title = s.emdash_task[:200]
                                binding.session.save(update_fields=["title"])
                except Exception:  # noqa: BLE001 — a title must never cost liveness
                    logger.warning("could not retitle session %s from task %r",
                                   binding.session_id, s.emdash_task, exc_info=True)
                # thread_key/host are the binding's durable IDENTITY. NEVER overwrite a
                # non-empty one — an existing binding may be owned by an agent/phone
                # thread (record_session) and this report loop must not steal it (see
                # the docstring above). But DO fill an EMPTY one: bindings predating the
                # SessionLink fold have host="" and can never satisfy
                # RunnerBinding.reusable_by (which requires runner AND host), so a chat
                # sent to one spawned a fresh emdash session forever instead of reusing
                # the live one. Fill-if-empty heals those without clobbering anything.
                if not binding.thread_key:
                    binding.thread_key = f"emdash:{s.emdash_task}"
                if not binding.host:
                    binding.host = runner.host
            binding.runner = runner
            binding.status = s.status or ""
            binding.last_interacted_at = _aware(s.last_interacted_at)
            binding.live_seen_at = timezone.now()
            binding.tail = list(s.recent_messages or [])
            binding.save()

    # Un-archive anything re-reported as open. The DERIVED staleness half of
    # `state=active` recomputes on every read, but this WRITTEN half does not heal
    # itself — without this, a task you reopened in emdash stays archived forever.
    if now_keys:
        Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=now_keys,
            status=Session.ARCHIVED,
        ).update(status=Session.ACTIVE)

    # Apply the closing signal. `now_keys` wins over `archived`: emdash task names are
    # not unique, so an open task must never be retired by an archived namesake.
    closed = [k for k in (archived or []) if k and k not in now_keys]
    if closed:
        Session.objects.filter(
            runner_binding__runner=runner,
            runner_binding__session_key__in=closed,
        ).update(status=Session.ARCHIVED)

    # NOTHING is cleared here. `RunnerBinding.runner` is durable IDENTITY — which box
    # this session lives on — and a session must never forget that just because its
    # task stopped being reported (emdash DELETES a closed task, so falling off the
    # report is the NORMAL end of life, not an anomaly). Liveness is `live_seen_at`,
    # stamped above on everything in this report and read against
    # SESSION_LIVE_WINDOW; see apps/canopy_sessions/staleness.py.
    #
    # Nulling the FK here is what left labs with 47 sessions that were listed as
    # active, could not say which runner they came from, and had no way back.

    # Fire AFTER commit so apps/realtime fans the durable rows (never racing the DB)
    # to the runner-owner's supervisor group — the WS push that makes live emdash
    # activity reach every connected viewer at once. Local import avoids a cycle.
    def _fire_reported():
        from apps.harness.signals import sessions_reported

        sessions_reported.send(sender=Runner, runner=runner)

    transaction.on_commit(_fire_reported)
    return len(deduped)


@dataclass
class SessionView:
    """The wire projection of a live runner session — the fields EmdashSessionOut
    reads. Derived from Session + RunnerBinding; preserves the frozen shape."""

    id: str
    emdash_task: str
    project: str
    status: str
    last_interacted_at: object
    recent_messages: list
    workspace_id: str
    runner_name: str


def list_visible_sessions(user) -> list[SessionView]:
    """Open sessions in the caller's workspaces whose runner is LIVE. Newest-first.

    Three conditions, all polled or explicit — none of them "the FK went null":
      * the session is not explicitly ARCHIVED (a decision, effective immediately),
      * its binding was in a report within SESSION_LIVE_WINDOW (the polled clock),
      * and its runner is still heartbeating (Runner.live_status).
    The last two overlap by design: a runner that stops heartbeating also stops
    reporting, so the strictest of the two wins and a dead box's rows go quiet fast.

    auto_join_workspaces runs first, mirroring list_turns: this is a flat-path
    handler (GET /api/harness/sessions), so WorkspaceResolveMiddleware's
    tenant-prefix auto-join never fires for it. Without this call, a
    domain-matching teammate who hasn't hit any other endpoint yet has no
    WorkspaceMembership row and user_workspace_slugs(user) returns empty,
    silently hiding their workspace's sessions instead of listing them.
    """
    from apps.canopy_sessions.models import RunnerBinding, Session

    wsvc.auto_join_workspaces(user)
    ws_slugs = wsvc.user_workspace_slugs(user)
    bindings = (
        RunnerBinding.objects.filter(
            runner__isnull=False,
            session__workspace_id__in=ws_slugs,
            session__status=Session.ACTIVE,
            live_seen_at__gte=stale_cutoff(),
        )
        .select_related("runner", "session")
        .order_by("-last_interacted_at")
    )
    out = []
    for b in bindings:
        if b.runner.live_status != Runner.ONLINE:
            continue
        out.append(
            SessionView(
                id=str(b.session_id),
                emdash_task=b.session_key,
                project=b.session.project,
                status=b.status,
                last_interacted_at=b.last_interacted_at,
                recent_messages=b.tail,
                workspace_id=b.session.workspace_id,
                runner_name=b.runner.name,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Items — the supervisor's queue (the dual of Turn)
# ---------------------------------------------------------------------------


class AlreadyDecidedError(Exception):
    """An item can be decided once. A second decision is a conflict (409), not a
    second dispatch."""


def create_items(*, agent, payloads: list[dict]) -> list[Item]:
    """Create items for an agent, idempotent per idempotency_key. A producer that
    re-posts its batch (a retried audit) gets the same rows back, not duplicates.

    The whole batch commits in ONE outer transaction so its post_save signals
    coalesce into a single push per agent (a fleet audit raising N items buzzes you
    once, not N times). Each item keeps its own SAVEPOINT so a single duplicate key
    replays without rolling back the batch — the idempotency guarantee is unchanged.
    """
    out: list[Item] = []
    with transaction.atomic():
        for p in payloads:
            key = p["idempotency_key"]
            existing = Item.objects.filter(idempotency_key=key).first()
            if existing is not None:
                out.append(existing)
                continue
            try:
                with transaction.atomic():  # savepoint — one dup doesn't sink the batch
                    out.append(Item.objects.create(
                        agent=agent,
                        kind=p.get("kind") or Item.REVIEW,
                        title=p["title"],
                        body=p.get("body") or "",
                        origin=p.get("origin") or Turn.ORIGIN_API,
                        origin_ref=p.get("origin_ref") or {},
                        dispatch=p.get("dispatch") or [],
                        batch_key=p.get("batch_key") or "",
                        idempotency_key=key,
                        raised_by_id=p.get("raised_by") or None,
                    ))
            except IntegrityError:
                replay = Item.objects.filter(idempotency_key=key).first()
                if replay is None:
                    raise
                out.append(replay)
    return out


def decide_item(
    item: Item, *, decision: str, comment: str, by: str, actor_workspace_slugs: set[str],
    decided_by_user=None,
) -> tuple[Item, list[Turn]]:
    """Resolve an open item. Only IMPLEMENT dispatches.

    A review needs a decision from the closed set; a question needs a non-empty
    answer (its `decision` stays blank). Deciding twice raises AlreadyDecidedError —
    the guard that stops a double-click becoming a second dispatch.

    `actor_workspace_slugs` is the set of workspaces the DECIDING human belongs to;
    it's the authorization for a cross-agent dispatch — you may only dispatch a
    turn onto an agent in a workspace you're a member of (the hard tenant boundary).
    """
    from .dispatch import dispatch as dispatch_item  # local: dispatch imports services

    if item.state != Item.OPEN:
        raise AlreadyDecidedError(f"item {item.id} is already {item.state}")

    if item.kind == Item.QUESTION:
        if not (comment or "").strip():
            raise ValueError("a question is resolved by its answer — comment must not be empty")
        decision = ""
    elif decision not in (Item.IMPLEMENT, Item.SKIP, Item.DEFER):
        raise ValueError(
            f"decision must be one of implement|skip|defer, got {decision!r}"
        )

    # ATOMIC, and this is the whole ballgame. dispatch() raises on a bad spec
    # (unknown target_agent). Committing the decision first would leave the item
    # DECIDED but undispatched — and since deciding twice is a 409, permanently
    # unfixable: the work silently never happens while the UI says you approved it.
    # Rolling back instead means a bad spec is a 422 on an item that is still OPEN,
    # retryable the moment the producer fixes it.
    with transaction.atomic():
        item.state = Item.DECIDED
        item.decision = decision
        item.comment = comment or ""
        item.decided_by = by
        item.decided_by_user = decided_by_user if getattr(decided_by_user, "is_authenticated", False) else None
        item.decided_at = timezone.now()

        turns: list[Turn] = []
        if decision == Item.IMPLEMENT:
            turns = dispatch_item(item, actor_workspace_slugs=actor_workspace_slugs)
            item.dispatched_at = timezone.now()

        item.save(update_fields=[
            "state", "decision", "comment", "decided_by", "decided_by_user",
            "decided_at", "dispatched_at",
        ])
    return item, turns


def dismiss_item(item: Item, *, by: str, decided_by_user=None, comment: str = "") -> Item:
    """Retire an OPEN item without acting — a producer that raised it in error, or
    a subject that changed under it. `comment` records WHY (e.g. an agent retracting
    a finding it verified was already shipped), so a dismissed row isn't a mystery.

    Only an OPEN item may be dismissed. Dismissing an already-DECIDED item would
    overwrite `decided_by`/`decided_at` — erasing who approved it — while the turns
    that decision already dispatched keep running: the queue would read "dismissed"
    for work that is executing, with the approver's identity gone. So dismiss guards
    on state exactly like decide does; a re-dismiss is likewise a 409, not a
    silent second write."""
    if item.state != Item.OPEN:
        raise AlreadyDecidedError(f"item {item.id} is already {item.state}")
    item.state = Item.DISMISSED
    item.decided_by = by
    item.decided_by_user = decided_by_user if getattr(decided_by_user, "is_authenticated", False) else None
    item.decided_at = timezone.now()
    fields = ["state", "decided_by", "decided_by_user", "decided_at"]
    if comment:
        item.comment = comment
        fields.append("comment")
    item.save(update_fields=fields)
    return item


# ---- Runner credentials (per-runner secret bundle, encrypted at rest) ----
def set_runner_credential(runner, *, claude_token=None, github_token=None,
                          op_sa_token=None, updated_by=None):
    """Upsert a runner's credential bundle. None fields are left unchanged."""
    from apps.common.encryption import encrypt_secret

    from .models import RunnerCredential

    cred, _ = RunnerCredential.objects.get_or_create(runner=runner)
    if claude_token is not None:
        cred.claude_token_enc = encrypt_secret(claude_token)
    if github_token is not None:
        cred.github_token_enc = encrypt_secret(github_token)
    if op_sa_token is not None:
        cred.op_sa_token_enc = encrypt_secret(op_sa_token)
    if updated_by is not None:
        cred.updated_by = updated_by
    cred.save()
    return cred


def get_runner_credential(runner) -> dict:
    """Decrypt a runner's bundle for the runner to consume. Empty when unset."""
    from apps.common.encryption import decrypt_secret

    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"claude_token": "", "github_token": "", "op_sa_token": "", "updated_at": None}
    return {
        "claude_token": decrypt_secret(cred.claude_token_enc),
        "github_token": decrypt_secret(cred.github_token_enc),
        "op_sa_token": decrypt_secret(cred.op_sa_token_enc),
        "updated_at": cred.updated_at,
    }


def runner_credential_status(runner) -> dict:
    """Masked view — which tokens are set, never their values."""
    cred = getattr(runner, "credential", None)
    if cred is None:
        return {"has_claude_token": False, "has_github_token": False,
                "has_op_sa_token": False, "updated_at": None}
    return {
        "has_claude_token": bool(cred.claude_token_enc),
        "has_github_token": bool(cred.github_token_enc),
        "has_op_sa_token": bool(cred.op_sa_token_enc),
        "updated_at": cred.updated_at,
    }
# ---------------------------------------------------------------------------
# Schedule nags — an unattended occurrence becomes a real Item (not a projection)
# ---------------------------------------------------------------------------


def _raise_schedule_nag(schedule, turn: Turn) -> None:
    """A grace-released (unattended) scheduled occurrence becomes a review Item.

    Its `implement` re-runs the schedule's prompt as a fresh turn — the generic
    Item action replaces the old bespoke "Run now" nag button. `skip`/`defer`
    (or `dismiss`) retire it. Idempotent per released turn: a re-raise of the same
    occurrence collapses on the idempotency key, and a later abandonment gets its
    own row (keyed by the new turn), so a dismissed nag can legitimately return.

    Honours the schedule's `notify` channel list — the "inbox" channel is what
    materializes this Item; a schedule that opts out raises nothing.
    """
    if "inbox" not in (schedule.notify or []):
        return
    create_items(agent=schedule.agent, payloads=[{
        "kind": Item.REVIEW,
        "title": f"Scheduled turn unattended: {schedule.name}",
        "body": (
            f"“{schedule.name}” fired but was left unattended past "
            f"{schedule.grace_minutes}m. Implement to run it now, or skip."
        ),
        "origin": Turn.ORIGIN_CRON,
        "origin_ref": {
            "schedule_id": schedule.id, "turn_id": str(turn.id), "kind": "schedule_nag",
        },
        "dispatch": [{
            "prompt": schedule.prompt,
            "origin": Turn.ORIGIN_MANUAL,
            "origin_ref": {"schedule_id": schedule.id, "manual": True},
            "routing": schedule.routing,
        }],
        "idempotency_key": f"sched-nag:{schedule.id}:{turn.id}",
    }])


def resolve_schedule_nags(schedule_id: int) -> int:
    """Dismiss every open nag for a schedule — a later occurrence finished, so the
    owed attention is discharged. Called from finish_turn on a DONE occurrence."""
    count = 0
    for item in Item.objects.filter(state=Item.OPEN, origin_ref__schedule_id=schedule_id):
        dismiss_item(item, by="system:schedule")
        count += 1
    return count


# ---------------------------------------------------------------------------
# Readiness drills — a hard-pinned, read-only doctor turn per (runner, agent),
# resolved by the drilled agent's own report callback (proving it can reach
# the control plane) or by the turn failing outright. See
# docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md.
# ---------------------------------------------------------------------------

DRILL_PROMPT = """READINESS DRILL — READ-ONLY. You are the agent "{agent_slug}".
Verify you can operate end-to-end in THIS environment, then report.

1. Confirm your working environment. If your agent repo is not checked out here,
   clone it (read-only credentials are staged in this environment).
2. Run your doctor / preflight / setup-verification checks. READ-ONLY mode:
   take NO outward action — no emails, no posts, no board writes, no deploys,
   no state mutations anywhere.
3. Report the result back to canopy-web (this callback is part of the drill —
   it proves this environment can reach the control plane):

   curl -s -X POST "{report_url}" \\
     -H "Authorization: Bearer $(cat ~/.claude/canopy/workbench-token 2>/dev/null || echo "${{CANOPY_TOKEN:-$CANOPY_PAT}}")" \\
     -H "Content-Type: application/json" \\
     -d '{{"outcome": "pass", "summary": "<one-paragraph findings>"}}'

   Use "outcome": "fail" if ANY check failed, and say which. Keep the summary to
   one paragraph. Do nothing after reporting."""


def start_drill(runner: Runner, agents: list) -> list[RunnerDrill]:
    """Fan a readiness drill out over `agents`: reset each (runner, agent)
    RunnerDrill to pending and enqueue one hard-pinned, read-only doctor turn
    per agent. Drills queue behind real executing turns (the one-executing-turn
    constraint) — they never interrupt live work."""
    drills: list[RunnerDrill] = []
    for agent in agents:
        drill, _ = RunnerDrill.objects.update_or_create(
            runner=runner, agent=agent,
            defaults={"outcome": RunnerDrill.OUTCOME_PENDING, "summary": "",
                      "finished_at": None, "started_at": timezone.now()},
        )
        report_url = f"{settings.CANOPY_PUBLIC_BASE_URL}/api/harness/drills/{drill.id}/report"
        turn, _created = enqueue_turn(
            agent=agent,
            origin=Turn.ORIGIN_DRILL,
            idempotency_key=f"drill:{runner.id}:{agent.slug}:{uuid.uuid4().hex[:8]}",
            prompt=DRILL_PROMPT.format(agent_slug=agent.slug, report_url=report_url),
            pinned_runner=runner,
        )
        drill.turn = turn
        drill.save(update_fields=["turn"])
        drills.append(drill)
    return drills


def report_drill(drill: RunnerDrill, *, outcome: str, summary: str) -> RunnerDrill:
    """The drilled agent's own callback — proves this environment can reach the
    control plane, not just run its checks locally."""
    if outcome not in (RunnerDrill.OUTCOME_PASS, RunnerDrill.OUTCOME_FAIL):
        raise ValueError(f"outcome must be pass|fail, got {outcome!r}")
    drill.outcome = outcome
    drill.summary = summary
    drill.finished_at = timezone.now()
    drill.save(update_fields=["outcome", "summary", "finished_at"])
    return drill


def seed_assignments_from_capabilities() -> int:
    """One-time bridge from the old two-sided routing config (runner
    capabilities.agents ∩ agent.runner_preference kind order) into explicit
    RunnerAssignment rows. Idempotent: skips (agent, runner) pairs that already
    have a row. Returns rows created. Used by the seed data migration."""
    from apps.agents.models import Agent
    from apps.harness.models import Runner, RunnerAssignment

    created = 0
    runners = list(Runner.objects.exclude(status=Runner.RETIRED))
    for agent in Agent.objects.all():
        matched = [r for r in runners if agent.slug in (r.capabilities.get("agents") or [])]
        pref = agent.runner_preference or []

        def sort_key(r):
            kind_rank = pref.index(r.kind) if r.kind in pref else len(pref)
            return (kind_rank, r.name)

        existing = set(
            RunnerAssignment.objects.filter(agent=agent).values_list("runner_id", flat=True)
        )
        next_rank = RunnerAssignment.objects.filter(agent=agent).count()
        for r in sorted(matched, key=sort_key):
            if r.id in existing:
                continue
            RunnerAssignment.objects.create(agent=agent, runner=r, rank=next_rank)
            next_rank += 1
            created += 1
    return created
