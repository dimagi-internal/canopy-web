"""The claim: a runner asks for work and gets at most one turn.

`claim_next_turn` is a single conditional UPDATE, so no row can be claimed twice.
Expired leases are swept on the off-request clock (`sweep_expired_leases`, run by
apps/harness/sweeps.py), not by the claim.
Its explanations live in `claim_reach.py` (re-exported here) because they must apply the
same rules: `turn_reach` (will a live runner pick this up? — what the turn status and
Slack say) and `unclaimable_queued_turns` (the web's stuck-turn warning).

Not `claim_payload.py`, which builds the payload a claimed turn is SENT as. Split out
of `services.py`.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.agents.holding import runner_may_hold_agent
from apps.canopy_sessions.models import RunnerBinding
from apps.harness import clis
from apps.harness import runner_requirements as rr
from apps.workspaces import services as wsvc

from .claim_reach import (  # noqa: F401  (the diagnostics live in claim_reach; callers still say `claim.X`)
    LIVE,
    OFFLINE,
    PARKED,
    SIGNED_OUT,
    UNCLAIMABLE_GRACE,
    UNROUTED,
    Reach,
    _assignment_rows_for_turns,
    _orders_for,
    _refined_allows,
    queued_turns_covered_by,
    runner_situation,
    turn_reach,
    turn_workspace_slug,
    unclaimable_queued_turns,
)
from .ledger import append_events
from .models import (
    FleetHold,
    Runner,
    RunnerAssignment,
    Turn,
    TurnEvent,
    WorkspaceRunnerOrder,
)
from .routing import (
    MIDTURN_FAILED,
    agent_tenant_q,
    assignment_allows,
    assignment_allows_for_agent,
    busy_session_ids,
    holds_a_job,
    is_project_session_turn,
    is_repo_turn,
    job_refusal,
    kind_allows,
    load_assignment_rows,
    profile_q,
    project_session_allows,
    repo_order_allows,
    ridable_sessions,
    runner_target_q,
    runner_tenant_slugs,
    transfer_held_q,
    turn_serves_refusal,
    turn_workspace_q,
)
from .turn_agent_tokens import revoke_stale
from .turn_close import close_turn

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 900


def _ago(when, now) -> str:
    seconds = int((now - when).total_seconds())
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def lease_lapse_note(turn: Turn, now) -> str:
    """Why an expired lease ended this turn, in words a person can act on: which
    runner held it, when that runner last heartbeat and when the lease ran out.

    A runner that is still heartbeating but stopped renewing THIS turn lost track
    of it (a restarted daemon that could not adopt it, a wedged worker); one whose
    heartbeats stopped too went away (stopped, crashed, powered off)."""
    runner = turn.claimed_by
    lapsed = turn.lease_expires_at
    lapsed_txt = f"{lapsed:%Y-%m-%d %H:%M:%S}Z ({_ago(lapsed, now)})" if lapsed else "unknown"
    if runner is None:
        return f"[canopy] lease expired at {lapsed_txt}; no runner held the turn"
    beat = runner.last_heartbeat_at
    if beat is None:
        beat_txt = "has never heartbeat"
        why = "the runner went away"
    else:
        beat_txt = f"last heartbeat {beat:%Y-%m-%d %H:%M:%S}Z ({_ago(beat, now)})"
        why = ("the runner is still heartbeating but stopped renewing this turn — it "
               "lost track of it (a restart that could not adopt it, or a wedged worker)"
               if lapsed is not None and beat > lapsed
               else "the runner stopped heartbeating (stopped, crashed or powered off)")
    return (f"[canopy] lost: runner {runner.name} ({runner.pk}) held this turn and its lease "
            f"expired at {lapsed_txt}; {beat_txt}. {why}.")[:2000]


def sweep_expired_leases() -> int:
    now = timezone.now()
    expired = list(
        Turn.objects.filter(
            status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
            lease_expires_at__lt=now,
        ).select_related("claimed_by")
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
        # A note the runner already left stands; otherwise the lapse is explained,
        # so a LOST turn never ends with an empty note (`lease_lapse_note`). Riders
        # are not ended with it — each holds its own lease and is swept on its own.
        note = None if (turn.result_note or "").strip() else lease_lapse_note(turn, now)
        if close_turn(turn, status, note, from_states=Turn.NON_TERMINAL,
                      where={"lease_expires_at__lt": now}, now=now,
                      event={"reason": "lease_expired"}, riders=False,
                      drill_summary=summary):
            count += 1
    # The catch-all: a turn requeued or ended by any other queryset update.
    revoke_stale()
    return count


def _capability_sweep() -> None:
    """apps/capabilities' sweep, behind a guard of its own: claiming must not
    depend on it even if its import fails."""
    try:
        from apps.capabilities.services import sweep_tick

        sweep_tick(trigger="claim")
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("capability sweep hook failed")


#: A runner's poll time is written at most this often: claiming is the hottest path in
#: the system and a timestamp precise to the second is not worth a write per call.
POLL_RECORD_EVERY_S = 15


def _record_poll(runner: Runner) -> None:
    """Note that `runner` asked for work just now. Never raises: observability must
    not be able to stop a claim."""
    try:
        now = timezone.now()
        last = runner.last_claim_poll_at
        if last is None or (now - last).total_seconds() >= POLL_RECORD_EVERY_S:
            Runner.objects.filter(pk=runner.pk).update(last_claim_poll_at=now)
            runner.last_claim_poll_at = now
    except Exception:  # noqa: BLE001
        logger.exception("could not record claim poll for runner %s", runner.pk)


#: Each distinct skip reason is said on a turn at most this often.
SKIP_NOTE_EVERY = dt.timedelta(minutes=10)
#: And a single claim call notes at most this many turns, so a deep queue costs a bounded amount.
SKIP_NOTES_PER_CLAIM = 5


def _note_skips(runner: Runner, skips: dict) -> None:
    """Say, on each turn this box passed over, WHY — as a `waiting` event the turn's
    own timeline shows. Throttled per (turn, box, reason). Never raises."""
    if not skips:
        return
    try:
        now = timezone.now()
        for turn_id, why in list(skips.items())[:SKIP_NOTES_PER_CLAIM]:
            recent = TurnEvent.objects.filter(turn_id=turn_id, kind="waiting",
                                              ts__gte=now - SKIP_NOTE_EVERY).order_by("-seq")
            if any((e.payload or {}).get("runner") == runner.name and (e.payload or {}).get("skipped") == why
                   for e in recent[:20]):
                continue
            turn = Turn.objects.filter(pk=turn_id, status=Turn.QUEUED).first()
            if turn is not None:
                append_events(turn, [{"kind": "waiting", "payload": {"runner": runner.name, "skipped": why}}])
    except Exception:  # noqa: BLE001
        logger.exception("could not note skipped turns for runner %s", runner.pk)


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
    _record_poll(runner)
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
    # A fleet member takes work only while in service: one the scaler is draining
    # (`fleets.py`) stays idle until it is scaled in on the next tick, and one
    # going into the warm pool would be stopped under the turn.
    if runner.fleet_id is not None and runner.power != Runner.POWER_ON:
        return None
    # A box whose Claude login is gone runs nothing: every turn it took would come
    # back as the CLI's "Not logged in" (2026-10-22, acedimagi-mbp-cdp answered
    # three messages that way). Not every not-ready note means that — a box with
    # no Claude desktop CLI still runs turns in emdash — so only this one gates.
    # Above the pin, like the pause: a pin cannot make a signed-out box work.
    # Per CLI (`harness.clis`): a box with Codex signed in still takes Codex turns
    # while its Claude login is gone, and `clis.choose` below keeps each turn to a
    # CLI the box has signed in. A box with none signed in takes nothing.
    reports = clis.reported(runner)
    if not any(r.signed_in for r in reports.values()):
        return None
    # A computer used in plain Claude Code has no runner app to run a claimed turn;
    # it never heartbeats, so the guard above already stops it — this says so.
    if runner.kind == Runner.COMPUTER:
        return None
    # The FLEET-WIDE hold (FleetHold): nothing is claimed, ridden or pinned anywhere
    # while it is on, and queued turns wait — visible, with their trigger — for the
    # release. Above pin matching for the same reason the runner pause is: a pin
    # must not punch through an operator's stop. The queue's sweeps (expired leases,
    # stale occurrences, late slots, stale drills) are not the claim's: they run on
    # the off-request clock (apps/harness/sweeps.py), and not while the hold is on.
    if FleetHold.is_held():
        return None
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

        if not has_pins and not RunnerBinding.objects.filter(runner=runner).exists():
            return None
    elif not has_assignments and not projects and not session_capable and not has_pins:
        return None
    routing_q = Q(routing__in=[Turn.PREFER_LOCAL, Turn.LOCAL_ONLY, Turn.ANY])
    if runner.kind == Runner.CLOUD:
        routing_q = Q(routing=Turn.ANY) | Q(routing=Turn.PREFER_LOCAL)
        # A cloud box takes a `cloud_only` turn (a job, or a turn that must not meet an
        # older box) only when it says it can: `job_refusal` below, which a pin does
        # not get past. The coarse query here only has to let it through to be judged.
        routing_q |= Q(routing=Turn.CLOUD_ONLY)
        # prefer_local turns fall to cloud only via the Phase 2 router policy;
        # Phase 0 has no cloud runners, so keep the simple rule: cloud never
        # takes local_only.
    # The busy set lives in `busy_session_ids` (with why its null filter is
    # load-bearing), shared with `blocking_turn` so the status line's "queued
    # behind the current turn" is this exclusion, not a second guess at it.
    # Only a CONVERSATION is a lane: an agent has none, and any number of its
    # turns execute at once (each on its own worktree, `cloud_runner`'s #1313).
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
        .exclude(chat_session_id__in=busy_sessions)
        # A session mid-transfer runs its transfer turn first (`transfer_held_q`).
        .exclude(transfer_held_q())
        # The AGENT hold (Agent.held): none of a held agent's turns — its own or a
        # chat with it — is claimed, ridden or pinned, like the fleet hold above
        # but for one agent. Outside `match_q` so a pin cannot punch through it.
        .exclude(agent__held=True)
        .exclude(chat_session__agent__held=True)
        # The WORKSPACE hold (Workspace.held): nothing in a held workspace, or one
        # below it, is claimed — same place, same reason (a pin cannot punch through).
        .exclude(turn_workspace_q(wsvc.held_slugs()))
        .filter(tenant_q)
        # assignment_allows reads turn.agent_id; the session leg's stickiness
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
    my_flags = rr.in_force(runner)
    # At most one job at a time per box (they share a Unix user): asked once, if a job
    # turn is among the candidates.
    job_busy = None
    trusted: dict[int, bool] = {}
    # Why each candidate this box passed over was passed over, said on the turn
    # (`_note_skips`) so "queued, no runner has claimed it" never again has to be
    # debugged from outside (2026-10-30: five silent hours).
    skips: dict = {}
    for turn in candidates:
        # Above the pin on purpose, like profile_q: a pin is a placement, never a
        # way past what the conversation's host requires of the box.
        if not rr.satisfies(my_flags, rr.requirements_of(turn)):
            skips[turn.pk] = "its conversation needs runner flags this box does not have"
            continue
        # A JOB (or any `cloud_only` turn) has an empty prompt that a box not built for
        # it would give to a model. Only a cloud box that reports `jobs` takes one, and
        # above the pin, the binding and the order like the checks around it: a pin
        # places work, it does not make a box able. Every claim comes through here —
        # REST and the socket — so this is the only door.
        if (why := job_refusal(runner, turn)) is not None:
            skips[turn.pk] = why
            continue
        if turn.job:
            if job_busy is None:
                job_busy = holds_a_job(runner)
            if job_busy:
                skips[turn.pk] = "this box is already running a job, and takes one at a time"
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
                skips[turn.pk] = (f"this box's owner is not an admin of {held.slug}, and only a box owned "
                                  f"by one of an agent's admins may run its turns")
                continue
        # What it serves on its machine, above the pin and the binding like the hold:
        # an agent's runner runs only turns that act as its agent (none of a repo's or an
        # agentless chat's), and an agent with a runner of its own runs on no other
        # runner of the machine — a pinned or bound turn would otherwise hand it the
        # agent's secrets (`device_keys.runner_serves_agent`).
        if turn_serves_refusal(runner, turn):
            skips[turn.pk] = "this box is dedicated to a different agent or repo"
            continue
        # The agent CLI it runs on (`clis.choose`), above the pin like the hold: a
        # pin cannot put a turn on a CLI the box lacks, or a caller's turn on one
        # that cannot confine it.
        cli = clis.choose(turn, runner, reports=reports)
        if cli is None:
            skips[turn.pk] = ("no agent CLI on this box can run it (not signed in, or it cannot confine "
                              "a caller's turn)")
            continue
        pinned_here = turn.pinned_runner_id == runner.id
        if not pinned_here:
            if not kind_allows(runner, turn.routing):
                skips[turn.pk] = f"its routing ({turn.routing}) does not allow a {runner.kind} runner"
                continue
            if turn.agent_id:
                if not assignment_allows(runner, turn, defaults, priorities, now):
                    skips[turn.pk] = (f"this box is not in {turn.agent.slug}'s runner order for "
                                      f"{turn.origin} turns, or a better-ranked runner is still available")
                    continue
            if is_repo_turn(turn) and not repo_order_allows(runner, turn, orders, now):
                skips[turn.pk] = "the workspace's runner order does not offer this repo turn to this box"
                continue
            # An unbound project chat: a box with its repo, that the workspace's
            # order shares or the chatting person administers (spec 2026-10-19).
            if is_project_session_turn(turn) and not project_session_allows(
                    runner, turn, orders, now, admins=admins):
                skips[turn.pk] = "the workspace's runner order does not offer this project chat to this box"
                continue
            if turn.chat_session_id:
                sess = turn.chat_session
                binding = getattr(sess, "runner_binding", None)
                bound_to_me = binding is not None and binding.runner_id == runner.id
                if not bound_to_me and sess.agent_id:
                    if not assignment_allows_for_agent(
                        runner, sess.agent_id, turn, defaults, priorities, now
                    ):
                        skips[turn.pk] = (f"this box is not in {sess.agent.slug}'s runner order for "
                                          f"{turn.origin} turns, or a better-ranked runner is still available")
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
        if rides is not None and cli != (Turn.objects.filter(pk=rides).values_list(
                "cli", flat=True).first() or clis.CLAUDE_CODE):
            # A rider is typed into the running session, so it runs on that session's
            # CLI or not at all (a turn claimed before CLIs ran on Claude Code); it
            # waits for the turn to end rather than switch the conversation's CLI.
            continue
        try:
            # Own atomic block per attempt: an IntegrityError from the
            # one_executing_turn_per_session index (concurrent claim for the
            # same conversation) must not poison an outer transaction.
            with transaction.atomic():
                updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
                    status=Turn.CLAIMED,
                    claimed_by=runner,
                    claimed_at=now,
                    lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
                    turn_mode=mode.mode if mode else "",
                    turn_mode_basis=mode.basis[:320] if mode else "",
                    rides_turn_id=rides,
                    cli=cli,
                    **({"origin_ref": turn.origin_ref} if stamp else {}),
                )
        except IntegrityError:
            continue  # another runner claimed for this conversation between our check and update
        if updated:
            # The conversation is held to this CLI only once the box reports a
            # session in it (`clis.pin_conversation`): a turn that never starts
            # leaves no history anywhere, and is picked for again.
            turn.refresh_from_db()
            if auto is not None:
                admission.record_auto(turn, auto, runner)
            append_events(turn, [{"kind": "status", "payload": {
                "status": Turn.CLAIMED, "runner": runner.name, "cli": cli,
                **_routing_basis(turn, runner, priorities),
                **({"rides_turn": str(rides)} if rides else {}),
            }}])
            _note_skips(runner, skips)
            return turn
    _note_skips(runner, skips)
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
