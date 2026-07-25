"""Django Ninja router for /api/harness — runner registry + turn lifecycle."""
from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import services
from .models import AgentSchedule, Runner, RunnerDrill, Turn
from .schedule_services import serialize_schedule
from .schemas import (
    BackfillSyncOut,
    BackfillWriteOut,
    DrillIn,
    DrillReportIn,
    EmdashSessionOut,
    UnclaimableTurnOut,
    HeartbeatIn,
    RecordSessionIn,
    ReportSessionsIn,
    ResolveSessionIn,
    ResolveSessionOut,
    RunnerCapabilitiesIn,
    RunnerCredentialIn,
    RunnerCredentialOut,
    RunnerCredentialStatusOut,
    RunnerDrillOut,
    RunnerIn,
    RunnerOut,
    ScheduleFireIn,
    ScheduleOut,
    SessionBackfillIn,
    SessionReportOut,
    SessionStreamIn,
    StreamPostOut,
    StreamSyncOut,
    TurnEventCountOut,
    TurnEventsIn,
    TurnEventsOut,
    TurnFinishIn,
    TurnIn,
    TurnOut,
    TurnStartIn,
)

router = Router(auth=session_auth, tags=["harness"])

# Allowed values for TurnEvent.kind. Kept in sync with the event kinds the
# runner/agent side actually emits; anything else 422s at the API boundary
# rather than being silently persisted.
ALLOWED_EVENT_KINDS = {
    "status",
    "assistant",
    "tool_start",
    "tool_end",
    "question",
    "approval",
    "error",
    "heartbeat",
    "cancel_requested",
}


def _agent_or_404(request: HttpRequest, slug: str) -> Agent:
    """Resolve an agent, gated by workspace membership. A non-member gets the
    same 404 as a missing agent (no existence leak). Domain users are auto-joined
    to the agent's workspace first, so the default-workspace case keeps working.

    Harness-local twin of agents.api._get_agent_or_404 — deliberately duplicated
    rather than imported: api modules must not depend on each other, and the
    harness is framework-tier.
    """
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws and agent.workspace_id != ws:
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant
    if agent.workspace_id and not wsvc.is_member(request.user, agent.workspace_id):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _runner_visibility_q(request: HttpRequest) -> Q:
    """The single definition of 'runners this caller can see and act on'.
    _runner_or_404 and list_runners MUST build from this — when they were
    written separately they drifted: the list OR'd in workspace_id__isnull=True
    unconditionally while the gate 404'd a null-workspace runner once a tenant
    was pinned, so the list showed a runner every action then 404'd on.

    Tenant-pinned (request.workspace_slug truthy): exact workspace match only —
    a null-workspace runner is wrong-tenant here, NOT ungated. No separate
    is_member check is needed in this branch: WorkspaceResolveMiddleware
    already gates membership of the pinned workspace before it ever sets
    request.workspace_slug, so a runner matching `ws` implies the caller is a
    member of it.

    Not pinned (flat /api/harness/... callers): the runner's workspace must be
    one the caller is a member of, or null (the legacy-ungated path
    pre-existing tests depend on).

    Either way, paired_by must be the caller, or null (also legacy-ungated).
    """
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws:
        wq = Q(workspace_id=ws)
    else:
        wq = Q(workspace_id__in=wsvc.user_workspace_slugs(request.user)) | Q(workspace_id__isnull=True)
    return wq & (Q(paired_by=request.user) | Q(paired_by__isnull=True))


def _runner_or_404(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    """Resolve a live runner via _runner_visibility_q — the same predicate
    list_runners filters on, so a runner that is listed is always one you can
    act on. Binding to runner.paired_by (not to a specific token) is
    deliberate: BearerTokenAuthMiddleware stamps request.user = token.user and
    discards which token was used, and PATs are rotated by design
    (canopy:canopy-web-pat-mint is documented "re-run to rotate"), so
    token-binding would break the runner on every rotation. Accepted residual:
    another token of the SAME user still works.
    """
    runner = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(_runner_visibility_q(request))
        .filter(pk=runner_id)
        .first()
    )
    if runner is None:
        raise HttpError(404, "runner not found")
    return runner


def _turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """Resolve a turn, gated by its tenant.

    An AGENT turn derives its tenant one hop away, via agent.workspace (spec
    section 8) — it has no workspace FK of its own, because denormalized tenancy
    drifts. A PROJECT turn has no agent to derive from, so it carries its own
    workspace FK and is gated on that instead. Same 404-not-403 rule either way:
    non-membership must not leak existence.
    """
    turn = Turn.objects.select_related("agent", "claimed_by").filter(pk=turn_id).first()
    if turn is None:
        raise HttpError(404, "turn not found")
    if turn.agent_id:
        _agent_or_404(request, turn.agent.slug)  # raises 404 on wrong tenant
        return turn

    # Project turn: gate on its own workspace, mirroring _agent_or_404's checks.
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws and turn.workspace_id != ws:
        raise HttpError(404, "turn not found")  # wrong tenant
    if turn.workspace_id and not wsvc.is_member(request.user, turn.workspace_id):
        raise HttpError(404, "turn not found")
    return turn


@router.post("/runners/", response={201: RunnerOut})
def pair_runner(request: HttpRequest, payload: RunnerIn):
    if payload.kind not in dict(Runner.KIND_CHOICES):
        raise HttpError(422, f"unknown runner kind '{payload.kind}'")
    wsvc.auto_join_workspaces(request.user)
    explicit = (payload.workspace or "").strip()
    if explicit:
        # Membership-gated: a missing workspace and a non-member get the same
        # 404 (no existence leak), exactly as apps/agents does on explicit homing.
        if not wsvc.is_member(request.user, explicit):
            raise HttpError(404, f"workspace '{explicit}' not found")
        ws_slug = explicit
    else:
        default = wsvc.user_default_workspace(request.user)
        ws_slug = default.slug if default else None
    runner = Runner.objects.create(
        name=payload.name,
        kind=payload.kind,
        capabilities=payload.capabilities,
        host=payload.host,
        paired_by=request.user,
        workspace_id=ws_slug,
    )
    return Status(201, runner)


@router.post("/runners/{runner_id}/credential", response=RunnerCredentialStatusOut,
             summary="Set a cloud runner's credential bundle (owner only)")
def set_runner_credential(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCredentialIn):
    """Store the per-runner secrets a cloud runner fetches at startup — its Claude
    login, a read-only GitHub token, the 1Password SA token. Owner-gated exactly
    like heartbeat/claim (paired_by == caller). Non-clobbering per field. Encrypted
    at rest; the response is masked (booleans, never values)."""
    runner = _runner_or_404(request, runner_id)
    services.set_runner_credential(
        runner,
        claude_token=payload.claude_token,
        github_token=payload.github_token,
        op_sa_token=payload.op_sa_token,
        updated_by=request.user,
    )
    return services.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential", response=RunnerCredentialOut,
            summary="Fetch this runner's credential bundle (the runner, via its PAT)")
def get_runner_credential(request: HttpRequest, runner_id: uuid.UUID) -> RunnerCredentialOut:
    """A cloud runner fetches its own secrets to stage into its environment. Returns
    the actual token values over HTTPS, gated to the runner's owner (paired_by ==
    caller) — the same trust boundary that lets that caller claim turns as the
    runner. Laptop/emdash runners never call this (they use ambient auth)."""
    runner = _runner_or_404(request, runner_id)
    return RunnerCredentialOut(**services.get_runner_credential(runner))


@router.get("/runners/", response=list[RunnerOut], summary="List my runners")
def list_runners(request: HttpRequest):
    """The supervisor's runner status. Filters on the exact same
    _runner_visibility_q predicate _runner_or_404 gates on — a runner you
    cannot act on must not be listed. Retired runners are excluded at lookup,
    as everywhere else."""
    qs = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(_runner_visibility_q(request))
        .prefetch_related("drills")
        .order_by(models.F("last_heartbeat_at").desc(nulls_last=True))
    )
    return list(qs[:50])


@router.patch("/runners/{runner_id}", response=RunnerOut)
def update_runner_capabilities(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCapabilitiesIn):
    """Replace a runner's capabilities (owner-gated via _runner_or_404).

    Capabilities are set at pairing and were otherwise immutable — the only way to
    add `projects` to an existing runner was to re-pair, which mints a NEW runner
    and orphans the old one's RunnerBindings. This lets a paired runner opt into
    driving repos (or new agents) in place. capabilities is a routing hint, not a
    security boundary (the workspace gates), so replacing it changes what the
    runner PULLS, never what it may reach.
    """
    runner = _runner_or_404(request, runner_id)
    runner.capabilities = payload.capabilities
    runner.save(update_fields=["capabilities"])
    return runner


@router.post("/runners/{runner_id}/retire", response={204: None})
def retire_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Retire a runner — permanent, not a liveness state (see Runner.live_status).
    Idempotent by construction: _runner_or_404 already excludes retired runners,
    so retiring an already-retired runner 404s at lookup rather than no-opping
    here."""
    runner = _runner_or_404(request, runner_id)
    runner.status = Runner.RETIRED
    runner.save(update_fields=["status"])
    return Status(204, None)


@router.post("/runners/{runner_id}/heartbeat", response=RunnerOut)
def runner_heartbeat(request: HttpRequest, runner_id: uuid.UUID, payload: HeartbeatIn):
    runner = _runner_or_404(request, runner_id)
    if payload.host and payload.host != runner.host:
        runner.host = payload.host
        runner.save(update_fields=["host"])
    return services.heartbeat(
        runner,
        active_turn_ids=payload.active_turn_ids,
        degraded=payload.degraded,
        note=payload.note,
        ready=payload.ready,
        ready_note=payload.ready_note,
        code_branch=payload.code_branch,
    )


@router.post("/runners/{runner_id}/claim", response={200: TurnOut, 204: None})
def claim_turn(request: HttpRequest, runner_id: uuid.UUID, paused: str = ""):
    """Claim the next eligible turn. `paused` is an optional comma-separated list of
    agent slugs the caller has locally paused (per-agent pause) — the server skips
    their queued turns so nothing is claimed-then-released. Omitted by older runners
    (backward-compatible: no exclusions)."""
    runner = _runner_or_404(request, runner_id)
    exclude = [s for s in (p.strip() for p in paused.split(",")) if s]
    turn = services.claim_next_turn(runner, exclude_slugs=exclude or None)
    if turn is None:
        return Status(204, None)
    return Status(200, turn)


def _project_workspace_or_404(request: HttpRequest, ws_slug: str):
    """Tenant-gate a project session's workspace, mirroring _agent_or_404 for the
    agent case. A project link has no agent to derive tenancy from, so without
    this any runner could read another user's rolling `summary` by guessing
    thread_key.

    The workspace is passed EXPLICITLY (from the turn the runner is executing, via
    TurnOut.workspace_slug), not derived from a default: the pairer may belong to
    several workspaces, and a project turn already carries the one it belongs to.
    The pairer must be a member of it. Same 404-not-403 rule: a non-member gets
    404, never a disclosure that the workspace exists.
    """
    wsvc.auto_join_workspaces(request.user)
    if not ws_slug or not wsvc.is_member(request.user, ws_slug):
        raise HttpError(404, "workspace not found")
    ws = Workspace.objects.filter(slug=ws_slug).first()
    if ws is None:
        raise HttpError(404, "workspace not found")
    return ws


@router.post("/runners/{runner_id}/resolve-session", response=ResolveSessionOut)
def resolve_session(request: HttpRequest, runner_id: uuid.UUID, payload: ResolveSessionIn):
    """Given (target, thread_key), tell THIS runner whether it can reuse an existing
    emdash session (it owns the live hint) or must spawn fresh + rehydrate context.
    Runner-scoped because reuse depends on the caller's macOS host."""
    runner = _runner_or_404(request, runner_id)
    if payload.project:
        ws = _project_workspace_or_404(request, payload.workspace)
        return services.resolve_session(
            None, payload.thread_key, runner, project=payload.project, workspace=ws
        )
    agent = _agent_or_404(request, payload.agent_slug)
    return services.resolve_session(agent, payload.thread_key, runner)


@router.post("/runners/{runner_id}/record-session", response=ResolveSessionOut)
def record_session(request: HttpRequest, runner_id: uuid.UUID, payload: RecordSessionIn):
    """Upsert the durable link and point its live-session hint at THIS runner/host,
    after a session was created or reused for the thread. Returns the fresh resolution."""
    runner = _runner_or_404(request, runner_id)
    if payload.project:
        ws = _project_workspace_or_404(request, payload.workspace)
        services.record_session(
            None, payload.thread_key, runner=runner, project=payload.project, workspace=ws,
            emdash_task_id=payload.emdash_task_id, session_id=payload.session_id,
            agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
        )
        return services.resolve_session(
            None, payload.thread_key, runner, project=payload.project, workspace=ws
        )
    agent = _agent_or_404(request, payload.agent_slug)
    services.record_session(
        agent, payload.thread_key, runner=runner,
        emdash_task_id=payload.emdash_task_id, session_id=payload.session_id,
        agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
    )
    return services.resolve_session(agent, payload.thread_key, runner)


@router.post("/runners/{runner_id}/sessions", response=SessionReportOut)
def report_sessions(request: HttpRequest, runner_id: uuid.UUID, payload: ReportSessionsIn):
    """The runner reports the open emdash sessions it can see. Wholesale per runner.
    Owner-gated via _runner_or_404 (404, not 403). Sessions are tenant-owned; they
    default to the runner's workspace (dimagi in practice), which the pairer is a
    member of by construction."""
    runner = _runner_or_404(request, runner_id)
    ws = runner.workspace
    if ws is None:
        raise HttpError(404, "runner has no workspace")
    count = services.replace_reported_sessions(
        runner, ws, payload.sessions, payload.archived
    )
    return SessionReportOut(count=count)


@router.get("/runners/{runner_id}/streams", response=StreamSyncOut)
def list_streams(request: HttpRequest, runner_id: uuid.UUID):
    """The sessions this runner should be tailing live (a viewer is attached). The
    observable half of attach/detach — the runner syncs this each tick and starts/
    stops tailers; the WS runner.stream frame is only a latency optimization."""
    from django.db.models import Max as _Max

    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, stream_desired=True)
        .exclude(session_key="")
        # The catch-up marker: on attach the runner ships every transcript record
        # AFTER the server's max persisted turn_index (None = stream-forward only).
        .annotate(_last_index=_Max("session__messages__turn_index"))
    )
    return {"streams": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.project, "last_index": b._last_index}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/session-stream", response=StreamPostOut)
def post_session_stream(request: HttpRequest, runner_id: uuid.UUID, payload: SessionStreamIn):
    """The runner ships live conversational events for a session it backs. For an
    origin=runner session, events carrying a transcript ordinal are PERSISTED as
    Message rows first (the transcript is the durable source — spec 2026-07-24),
    then the assistant frames fan out to the session group as the same
    chat.turn_event frames the chat path uses (turn-less -> the consumer derives
    seq:<n> message ids). User events are persisted but never live-pushed — the
    sender's client already echoed them optimistically."""
    from apps.canopy_sessions import services as chat_services
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.realtime import groups

    runner = _runner_or_404(request, runner_id)
    binding = (
        RunnerBinding.objects.select_related("session")
        .filter(session_id=payload.session_id, runner=runner).first()
    )
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    if binding.session.origin == Session.ORIGIN_RUNNER:
        # Ordinal-less events (an old runner) stay live-view-only: persisting
        # assistant rows without the user side would blank the tail fallback's
        # human half the moment any row exists.
        chat_services.persist_transcript_rows(binding.session, [
            {"index": e.index, "role": e.kind, "text": (e.payload or {}).get("text", "")}
            for e in payload.events if e.index >= 0
        ])
    sgroup = groups.session_group(payload.session_id)
    n = 0
    for e in payload.events:
        if e.kind != "user":
            groups.publish(sgroup, {
                "type": "chat.turn_event",
                "event": {"kind": e.kind, "seq": e.seq, "payload": e.payload},
                "turn_id": None,
            })
        n += 1
    return {"count": n}


@router.get("/runners/{runner_id}/backfills", response=BackfillSyncOut)
def list_backfills(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions this runner has been asked to ship full history for."""
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, backfill_requested=True)
    )
    return {"backfills": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.project}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/session-backfill", response=BackfillWriteOut)
def post_session_backfill(request: HttpRequest, runner_id: uuid.UUID, payload: SessionBackfillIn):
    """The runner ships a session's full transcript; the server writes Message rows
    once and clears the request. Runner-owned-binding gated."""
    from apps.canopy_sessions import services as chat_services
    from apps.canopy_sessions.models import RunnerBinding, Session

    runner = _runner_or_404(request, runner_id)
    binding = RunnerBinding.objects.filter(session_id=payload.session_id, runner=runner).first()
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    session = Session.objects.get(pk=payload.session_id)
    written = chat_services.write_backfill(session, [m.dict() for m in payload.messages])
    binding.backfill_requested = False
    binding.save(update_fields=["backfill_requested", "updated_at"])
    return {"written": written}


@router.get("/turns/unclaimable", response=list[UnclaimableTurnOut],
            summary="Queued turns no online runner can claim")
def list_unclaimable_turns(request: HttpRequest):
    """A queued turn addressed to an agent/repo nothing declares sits forever with
    no signal (one sat 12h). Surfacing it turns a silent stall into a warning."""
    return services.unclaimable_queued_turns(request.user)


@router.post("/turns/", response={200: TurnOut, 201: TurnOut})
def enqueue_turn(request: HttpRequest, payload: TurnIn):
    if bool(payload.agent_slug) == bool(payload.project):
        raise HttpError(422, "a turn targets an agent_slug XOR a project")
    if payload.origin not in dict(Turn.ORIGIN_CHOICES):
        raise HttpError(422, f"unknown origin '{payload.origin}'")
    if payload.routing not in dict(Turn.ROUTING_CHOICES):
        raise HttpError(422, f"unknown routing '{payload.routing}'")

    agent = workspace = None
    if payload.agent_slug:
        agent = _agent_or_404(request, payload.agent_slug)
    else:
        # A project turn carries its own tenant.
        wsvc.auto_join_workspaces(request.user)
        ws_slug = getattr(request, "workspace_slug", None)
        if ws_slug:
            # current_workspace gates membership on an explicit slug, so a
            # non-member's enqueue cannot land in someone else's workspace. 404
            # rather than 403: the harness must not leak which tenants exist
            # (same rule as _agent_or_404).
            try:
                workspace = wsvc.current_workspace(request.user, ws_slug)
            except ValueError:
                raise HttpError(404, "workspace not found")
        else:
            workspace = wsvc.user_default_workspace(request.user)
            if workspace is None:
                # None means 0 memberships OR 2+ (ambiguous), and the two deserve
                # different answers. A 404 for the ambiguous case is a lie that
                # cost real debugging time: the flat shim 404'd every project
                # enqueue for a 2-workspace user (which the actual prod user is)
                # while reporting "not found". There is nothing to leak here —
                # they are the caller's OWN workspaces — so name the fix.
                if wsvc.user_workspace_slugs(request.user):
                    raise HttpError(
                        422,
                        "you belong to multiple workspaces; enqueue via "
                        "/api/w/{workspace}/harness/turns/",
                    )
                raise HttpError(404, "workspace not found")

    turn, created = services.enqueue_turn(
        agent=agent,
        project=payload.project,
        workspace=workspace,
        origin=payload.origin,
        idempotency_key=payload.idempotency_key,
        prompt=payload.prompt,
        origin_ref=payload.origin_ref,
        routing=payload.routing,
        enqueued_by=request.user,  # the human launching a manual / composer turn
    )
    return Status(201 if created else 200, turn)


@router.get("/turns/", response=list[TurnOut])
def list_turns(
    request: HttpRequest,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
):
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    qs = Turn.objects.select_related("agent", "claimed_by").order_by("-created_at")
    if agent:
        qs = qs.filter(agent__slug=agent)
    if status:
        qs = qs.filter(status__in=status.split(","))
    # Tenant filter: a turn's tenant is its agent's. Null-workspace agents stay
    # visible (ungated, per the migration-safety rule).
    qs = qs.filter(Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True))
    limit = max(1, min(limit, 200))  # clamp; default 100 keeps existing callers unchanged
    return list(qs[:limit])  # filter BEFORE slicing — a sliced queryset cannot be filtered


@router.get("/sessions", response=list[EmdashSessionOut])
def list_sessions(request: HttpRequest):
    """Open emdash sessions the caller can see — across their workspaces, live runners
    only, newest-first. Drives the phone's Open Sessions list."""
    return services.list_visible_sessions(request.user)


@router.get("/turns/{turn_id}", response=TurnOut)
def get_turn(request: HttpRequest, turn_id: uuid.UUID):
    return _turn_or_404(request, turn_id)


@router.post("/turns/{turn_id}/events", response=TurnEventCountOut)
def append_turn_events(request: HttpRequest, turn_id: uuid.UUID, payload: TurnEventsIn):
    turn = _turn_or_404(request, turn_id)
    for event in payload.events:
        if event.kind not in ALLOWED_EVENT_KINDS:
            raise HttpError(422, f"unknown event kind '{event.kind}'")
    count = services.append_events(turn, [e.dict() for e in payload.events])
    return {"count": count}


@router.get("/turns/{turn_id}/events", response=TurnEventsOut)
def read_turn_events(request: HttpRequest, turn_id: uuid.UUID, after: int = 0):
    turn = _turn_or_404(request, turn_id)
    events = turn.events.filter(seq__gt=after).order_by("seq")[:500]
    return {"events": list(events)}


@router.post("/turns/{turn_id}/start", response=TurnOut)
def start_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnStartIn):
    turn = _turn_or_404(request, turn_id)
    if turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        raise ProblemError(409, "Turn not startable", detail=f"status={turn.status}")
    return services.mark_running(turn, session_id=payload.session_id)


@router.post("/turns/{turn_id}/finish", response=TurnOut)
def finish_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnFinishIn):
    turn = _turn_or_404(request, turn_id)
    if payload.status not in (Turn.DONE, Turn.FAILED):
        raise HttpError(422, "finish status must be done|failed")
    if turn.status in Turn.TERMINAL:
        return turn  # idempotent finish
    result = services.finish_turn(turn, status=payload.status, result_note=payload.result_note)
    if result.status not in Turn.TERMINAL:
        # services.finish_turn only transitions claimed/running/needs_human — a
        # queued turn is a silent no-op there. Surface that as a 409 instead of
        # returning a turn that looks unchanged.
        raise ProblemError(409, "Turn not finishable", detail=f"status={result.status}")
    return result


@router.post("/turns/{turn_id}/cancel", response=TurnOut)
def cancel_turn(request: HttpRequest, turn_id: uuid.UUID):
    """Cancel a QUEUED turn that has not started — the misfire case the phone
    composer needs (dispatch the wrong command, take it back before a runner
    claims it). Finishes it CANCELLED with a cancelled note.

    QUEUED only. A claimed/running turn is already executing in an emdash session;
    stopping that is a different, racier operation (the runner owns the lease) —
    see `services.cancel_turn`, which signals the runner instead and is
    deliberately not wired to this route yet.
    """
    turn = _turn_or_404(request, turn_id)
    if turn.status in Turn.TERMINAL:
        return turn  # idempotent
    cancelled = services.cancel_queued_turn(turn)
    if cancelled is None:
        raise ProblemError(
            409, "Turn not cancelable",
            detail=f"status={turn.status}; only a queued turn can be cancelled",
        )
    return cancelled


# --------------------------------------------------------------------------------------
# AgentSchedule — the runner-facing half. The supervisor's CRUD lives in api_schedules.py;
# these two routes are what the laptop daemon actually calls: sync, then report a due slot.
# --------------------------------------------------------------------------------------

def _runner_schedule_qs(runner: Runner):
    """Schedules this runner may see, gated by TENANT — never by capabilities.

    capabilities is a caller-supplied routing hint declared at pairing and never
    validated (see b4f5ead, Critical): scoping by it would let anyone pair a
    runner declaring a victim's agent slug and read that agent's schedules,
    leaking `prompt`. The workspace is the boundary.

    The tenant is derived from `paired_by` — the human who paired the runner —
    rather than the Runner.workspace FK, because paired_by is server-assigned at
    pairing (request.user), so the FIELD is not attacker-controlled.

    That last point is necessary but NOT sufficient, and reading it alone is how
    this route was first shipped vulnerable. Deriving the tenant from an
    unspoofable field on a row the ATTACKER SELECTED buys nothing: runner_id is
    a caller-supplied query param, so choosing whose paired_by gets read is as
    good as spoofing it. The real invariant needs both halves — the tenant
    derives from paired_by AND _runner_or_404 pins the runner to request.user,
    so the row and the field are alike server-controlled.

    claim_next_turn NOW DERIVES THE SAME WAY (agent.workspace ∈
    workspaces(paired_by), or IS NULL), so the two rules AGREE: every schedule
    this runner may fire produces a turn that same runner may claim.

    They briefly diverged, and the divergence was an outage, not a nicety.
    claim_next_turn shipped scoped to the Runner.workspace FK while this
    predicate derived from paired_by — so a runner homed to `alpha` whose pairer
    also belongs to `beta` could SEE and FIRE beta's schedules here but could not
    CLAIM the resulting turns, leaving them QUEUED forever. Because one laptop
    runner serves a fleet that deliberately spans workspaces, that stopped 4 of 5
    production agents from executing at all. The resolution was to converge the
    CLAIM onto paired_by (this predicate's rule), NOT to narrow this one onto the
    FK: the FK records where a runner lives, not who it may work for.

    NULL paired_by fails closed below (none()), which is stricter than
    _runner_visibility_q's legacy-ungated allowance — an orphaned runner can be
    operated, but can never sync or fire a schedule.
    """
    qs = AgentSchedule.objects.filter(enabled=True).select_related("agent")
    if runner.paired_by_id is None:
        return qs.none()  # an orphaned runner has no identity to derive tenancy from
    slugs = wsvc.user_workspace_slugs(runner.paired_by)
    # Same-tenant agents, or legacy null-workspace agents (the pre-tenancy path
    # the existing suite covers). claim_next_turn's predicate is now the same
    # rule, deriving from the same paired_by — keep the two in step.
    return qs.filter(Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True))


@router.get("/schedules/", response=Page[ScheduleOut],
            summary="Schedules this runner may fire (tenant-scoped)")
def sync_schedules(request: HttpRequest, runner_id: uuid.UUID, limit: int = 200) -> Page[ScheduleOut]:
    """The runner's schedule sync. It caches these locally, evaluates the cron
    itself, and POSTs /fire when a slot comes due.

    Deliberately NOT gated on Runner.ONLINE, unlike claim_next_turn: ONLINE gates
    claiming because claiming ASSIGNS work and takes the one_executing_turn_per_agent
    lock, so an offline claimer would wedge the agent. Sync is a read, and fire only
    produces a QUEUED turn (which stacks freely and is executed by whichever ONLINE
    runner in the tenant claims it). Gating here would impose a boot-order dependency
    — a fresh daemon would have to sync before its first heartbeat.
    """
    runner = _runner_or_404(request, runner_id)
    items = [ScheduleOut(**serialize_schedule(s)) for s in _runner_schedule_qs(runner)]
    return paginate(items, offset=0, limit=clamp_limit(limit))


@router.post("/schedules/{schedule_id}/fire", response={201: TurnOut},
             summary="Report a due slot; the server materializes the turn")
def fire_schedule_route(
    request: HttpRequest, schedule_id: int, runner_id: uuid.UUID, payload: ScheduleFireIn
) -> Status:
    runner = _runner_or_404(request, runner_id)
    schedule = _runner_schedule_qs(runner).filter(pk=schedule_id).first()
    if schedule is None:
        # 404 whether it is missing, disabled, or another tenant's — no existence leak.
        raise HttpError(404, f"schedule {schedule_id} not found")
    # Deliberately does NOT call release_stale_occurrence_turns: fire_schedule already
    # supersedes every open occurrence, so release would add nothing here except
    # a self-destruct on same-slot re-fire (fire skips supersede when the key
    # exists, but release would already have killed the turn this route returns).
    # Release runs on the CLAIM tick instead — see claim_next_turn.
    turn, _ = services.fire_schedule(schedule, payload.slot)
    return Status(201, turn)


# --------------------------------------------------------------------------------------
# Readiness drills — a hard-pinned, read-only doctor turn per (runner, agent), resolved
# by the drilled agent's own report callback or by the turn failing (see
# services.start_drill / finish_turn). Spec 2026-07-24-directed-runner-routing, Task 7.
# --------------------------------------------------------------------------------------


@router.post("/runners/{runner_id}/drill", response=list[RunnerDrillOut])
def start_runner_drill(request: HttpRequest, runner_id: uuid.UUID, payload: DrillIn):
    """Fan out a readiness drill (owner-gated). Default: every agent assigned to
    this runner; body.agents narrows by slug. Deliberately includes DISABLED
    assignment rows too — drill-before-enable is the intended workflow (prove a
    standby actually works before flipping it live), so a disabled row must
    stay drillable even though it can never claim routed traffic."""
    runner = _runner_or_404(request, runner_id)
    # No enabled=True filter here on purpose — see the docstring above.
    assigned = Agent.objects.filter(runner_assignments__runner=runner)
    # `is not None` (not truthy) so an explicit [] narrows to "drill nothing" and
    # hits the 422 below, rather than being treated the same as "drill everyone".
    agents = list(assigned.filter(slug__in=payload.agents) if payload.agents is not None else assigned)
    if not agents:
        raise HttpError(422, "no assigned agents to drill — assign this runner to an agent first")
    return services.start_drill(runner, agents)


@router.get("/runners/{runner_id}/drills", response=list[RunnerDrillOut])
def list_runner_drills(request: HttpRequest, runner_id: uuid.UUID):
    runner = _runner_or_404(request, runner_id)
    return list(runner.drills.select_related("agent"))


@router.post("/drills/{drill_id}/report", response=RunnerDrillOut)
def report_drill(request: HttpRequest, drill_id: int, payload: DrillReportIn):
    """The drilled agent's callback. Gated like every runner route: the caller
    must be the drilled runner's owner (the agent runs under the owner's
    environment token, so this proves control-plane reachability too)."""
    drill = get_object_or_404(
        RunnerDrill.objects.select_related("runner", "agent"), pk=drill_id
    )
    _runner_or_404(request, drill.runner_id)  # reuse the owner gate; 404 on non-owner
    return services.report_drill(drill, outcome=payload.outcome, summary=payload.summary)
