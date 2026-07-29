"""Django Ninja router for /api/harness — runner registry + turn lifecycle."""
from __future__ import annotations

import uuid

from django.db import models, transaction
from django.db.models import Q
from django.http import HttpRequest, StreamingHttpResponse
from django.utils import timezone
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
from .models import AgentSchedule, Runner, RunnerAssignment, RunnerDrill, Turn
from .schedule_services import serialize_schedule
from .schemas import (
    BackfillSyncOut,
    BackfillWriteOut,
    DrillIn,
    DrillReportIn,
    EmdashSessionOut,
    UnclaimableTurnOut,
    HeartbeatIn,
    PauseIn,
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
    TranscriptAppendIn,
    TranscriptAppendOut,
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

# A runner is expected to flush the raw transcript periodically (never holding
# a whole run in memory — see cloud_runner's design), so a well-behaved batch
# is at most tens of KB. 1MB per request is generous headroom above that while
# still bounding a runaway/misbehaving batch rather than accepting an
# unbounded body straight into a gzip+DB write under the turn row lock.
#
# Deliberately well under settings.DATA_UPLOAD_MAX_MEMORY_SIZE (pinned
# explicitly to 2.5MB, config/settings/base.py — security review 2026-07-26,
# F8) rather than close to it: a request whose JSON-encoded body crosses THAT
# ceiling never reaches this view at all — request.body raises
# RequestDataTooBig as an unhandled 500 before Ninja even parses the payload.
# Keeping this cap well below it means an oversized batch always surfaces as
# our clean 422, not an occasional 500 depending on JSON escaping overhead.
TRANSCRIPT_APPEND_MAX_BYTES = 1 * 1024 * 1024


def _agent_or_404(request: HttpRequest, slug: str) -> Agent:
    """Resolve an agent, gated by workspace membership. A non-member gets the
    same 404 as a missing agent (no existence leak). Domain users are auto-joined
    to the agent's workspace first, so the default-workspace case keeps working.

    Harness-local twin of agents.api._get_agent_or_404 — deliberately duplicated
    rather than imported: api modules must not depend on each other, and the
    harness is framework-tier.

    Fails CLOSED on a workspace-less agent (security review 2026-07-26, F1):
    `agent.workspace_id` falsy must not short-circuit to "ungated" — that would
    hand ANY authenticated user (not just a workspace member) full read/write on
    a null-workspace agent's turns, including this app's own raw transcripts.
    Latent today (production has zero agents with workspace_id IS NULL), but a
    fail-open tenancy gate is a bug regardless of whether anything currently
    exploits it. A pre-migration agent with no workspace is simply not
    resolvable via this API until it's backfilled a workspace — it does not
    fall back to "visible to everyone."
    """
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws and agent.workspace_id != ws:
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant
    if not agent.workspace_id or not wsvc.is_member(request.user, agent.workspace_id):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _runner_owned_q(request: HttpRequest) -> Q:
    """Ownership: the caller paired it, or nobody did (legacy-ungated)."""
    return Q(paired_by=request.user) | Q(paired_by__isnull=True)


def _runner_read_q(request: HttpRequest) -> Q:
    """'Runners this caller can SEE' — derived from the TENANT, like claim time.

    Seeing a runner and acting on one are different questions that one predicate
    answered, and its `paired_by == caller` leg is right for the second and wrong
    for the first. A workspace's fleet is typically paired by ONE human, so every
    other member listed ZERO runners and could not distinguish "no runner serves
    this repo" from "I can see nothing at all".

    That cost a real afternoon (labs, 2026-07-28). `canopy project dispatch`
    preflights by listing the fleet; under an agent identity the list came back
    empty, it concluded BLOCKED, and it was routed around with `--no-preflight`.
    The next dispatch went at a repo nothing declared, was accepted 201, and sat
    QUEUED until the stuck-turn banner caught it — a guard that cries wolf gets
    disabled, and takes the true positives with it.

    `services.unclaimable_queued_turns` had ALREADY made this exact fix at its own
    call site, with a comment explaining that scoping candidates to
    `paired_by=user` made every stuck turn read as `config` for anyone who had not
    personally paired a runner. Same rule, second call site.

    What is deliberately NOT inherited from the act-on predicate: its
    `workspace_id__isnull=True` leg. There it is backstopped by ownership, so it
    means "your own legacy runner"; here, with ownership gone, the same leg would
    mean "everyone's", which is the NULL-means-allow shape this codebase has
    already paid to remove from six tenancy predicates (PRs #378, #421, #423). A
    runner with no workspace has no tenant to share, so it stays visible only to
    the human who paired it — hence the `& _runner_owned_q` on that leg alone.

    Nothing listed is secret to a member: RunnerOut carries status, capabilities,
    host and `paired_by_email` — never credentials, which have their own
    owner-gated route.
    """
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws:
        # Tenant-pinned: exact match only, and no null-workspace leg at all — a
        # null-workspace runner is wrong-tenant here, not ungated (the property
        # test_pinned_null_workspace_runner_is_neither_listed_nor_actionable
        # pins). WorkspaceResolveMiddleware has already gated membership of `ws`.
        return Q(workspace_id=ws)
    return (
        Q(workspace_id__in=wsvc.user_workspace_slugs(request.user))
        | (Q(workspace_id__isnull=True) & _runner_owned_q(request))
    )


def _runner_visibility_q(request: HttpRequest) -> Q:
    """'Runners this caller can ACT ON' — the tenant AND ownership. Unchanged.

    Heartbeating, claiming as, mutating, retiring and crediting a runner gate on
    this. Ownership is the boundary because these operations speak FOR the runner:
    `paired_by` is what `claim_next_turn` derives a tenant from, so acting as
    someone else's runner is acting with their memberships.

    `_runner_read_q` is deliberately WIDER, which gives up the invariant these two
    used to share by being one function ("never list a runner every action then
    404s on"). That invariant is now carried explicitly instead of structurally,
    by `RunnerOut.can_manage`: a client can say "runner X serves this repo, ask
    its owner to declare on it" rather than discovering ownership from a bare 404
    on an action it was told to try. Read ⊇ act-on holds by construction — every
    leg here appears in the read, ANDed with less.
    """
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if ws:
        wq = Q(workspace_id=ws)
    else:
        wq = Q(workspace_id__in=wsvc.user_workspace_slugs(request.user)) | Q(workspace_id__isnull=True)
    return wq & _runner_owned_q(request)


def _runner_or_404(
    request: HttpRequest, runner_id: uuid.UUID, *, include_retired: bool = False
) -> Runner:
    """Resolve a live runner via _runner_visibility_q — the same predicate
    list_runners filters on, so a runner that is listed is always one you can
    act on. Binding to runner.paired_by (not to a specific token) is
    deliberate: BearerTokenAuthMiddleware stamps request.user = token.user and
    discards which token was used, and PATs are rotated by design
    (canopy:canopy-web-pat-mint is documented "re-run to rotate"), so
    token-binding would break the runner on every rotation. Accepted residual:
    another token of the SAME user still works.

    `include_retired` is for the ONE operation that must reach a retired runner:
    un-retiring it. Everything else keeps 404ing, so retirement still means
    "invisible and inert" everywhere it matters.
    """
    qs = Runner.objects.all() if include_retired else Runner.objects.exclude(status=Runner.RETIRED)
    runner = qs.filter(_runner_visibility_q(request)).filter(pk=runner_id).first()
    if runner is None:
        raise HttpError(404, "runner not found")
    return runner


def _turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """Resolve a turn, gated by its tenant.

    An AGENT turn derives its tenant one hop away, via agent.workspace (spec
    section 8) — it has no workspace FK of its own, because denormalized tenancy
    drifts. A SESSION turn similarly derives its tenant via chat_session.workspace.
    A PROJECT turn has no agent/session to derive from, so it carries its own
    workspace FK and is gated on that instead. Same 404-not-403 rule either way:
    non-membership must not leak existence.

    Every rejection here raises the SAME uniform `HttpError(404, "turn not
    found")` — including the agent-turn branch, which delegates to
    `_agent_or_404` (security review 2026-07-26, F4): that helper's own 404
    names the agent (`"agent 'ada' not found"`), and the shared error handler
    copies an HttpError's message into both `title` and `detail`. Left
    un-caught, a caller holding a stale/guessed turn UUID would learn not just
    that the turn exists, but which agent owns it. Catching and re-raising
    here keeps every turn-not-resolvable case indistinguishable from the
    others, from the caller's side.
    """
    turn = (
        Turn.objects.select_related("agent", "claimed_by", "chat_session")
        .filter(pk=turn_id)
        .first()
    )
    if turn is None:
        raise HttpError(404, "turn not found")
    if turn.agent_id:
        try:
            _agent_or_404(request, turn.agent.slug)  # raises on wrong tenant
        except HttpError:
            raise HttpError(404, "turn not found") from None
        return turn

    # Session turn: tenancy derives from the chat session's workspace (a session
    # turn has agent_id=None AND workspace_id=None, so without this branch both
    # guards below fall through — any authenticated user could read the transcript).
    wsvc.auto_join_workspaces(request.user)
    ws = getattr(request, "workspace_slug", None)
    if turn.chat_session_id:
        slug = turn.chat_session.workspace_id
        if (ws and slug != ws) or not wsvc.is_member(request.user, slug):
            raise HttpError(404, "turn not found")
        return turn

    # Project turn: gate on its own workspace, mirroring _agent_or_404's checks.
    if ws and turn.workspace_id != ws:
        raise HttpError(404, "turn not found")  # wrong tenant
    # Fails CLOSED on a workspace-less project turn (security review
    # 2026-07-26, adjacent to F1): `enqueue_turn` always assigns a real
    # workspace to a project turn, so this is practically unreachable today —
    # but matches the invariant F1 established for the agent-turn branch
    # above, rather than leaving one fail-open gate three lines below a
    # fail-closed one.
    if not turn.workspace_id or not wsvc.is_member(request.user, turn.workspace_id):
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
        # A runner MUST belong to a workspace: a workspace-less one is
        # half-broken with no signal — heartbeat and claim work (tenancy
        # derives from paired_by), but every session report 404s, so its
        # sessions silently never surface (prod incident 2026-07-25: a
        # multi-workspace pairer made user_default_workspace() None and the
        # runner paired NULL). Fail loud instead of pairing broken.
        default = wsvc.user_default_workspace(request.user)
        if default is None:
            n = len(wsvc.user_workspace_slugs(request.user))
            detail = (
                "you belong to no workspace" if n == 0
                else f"you belong to {n} workspaces, so there is no default"
            )
            raise HttpError(
                422,
                f"a runner must belong to a workspace and {detail} — "
                "pass `workspace` explicitly",
            )
        ws_slug = default.slug
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


@router.get("/runners/", response=list[RunnerOut], summary="List the fleet I can see")
def list_runners(request: HttpRequest):
    """The supervisor's runner status, and the fleet read every preflight makes.

    Scoped by TENANT (`_runner_read_q`), not by who paired what: a member who
    paired nothing used to list nothing, which reads identically to "this
    workspace has no runners" and is the wrong answer to draw a conclusion from.
    Each row carries `can_manage` for the ownership half. Retired runners are
    excluded at lookup, as everywhere else.
    """
    qs = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(_runner_read_q(request))
        .prefetch_related("drills")
        .order_by(models.F("last_heartbeat_at").desc(nulls_last=True))
    )
    rows = list(qs[:50])
    for r in rows:
        # Resolved here rather than in the schema because it is a property of the
        # (caller, runner) PAIR, and a Ninja resolver only sees the row.
        r.can_manage = r.paired_by_id in (request.user.id, None)
    return rows


@router.patch("/runners/{runner_id}", response=RunnerOut)
def update_runner_capabilities(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCapabilitiesIn):
    """Replace a runner's capabilities (owner-gated via _runner_or_404).

    Capabilities are set at pairing and were otherwise immutable — the only way to
    add a capability to an existing runner was to re-pair, which mints a NEW runner
    and orphans the old one's RunnerBindings. This lets a paired runner opt into
    driving new agents in place. capabilities is a routing hint, not a security
    boundary (the workspace gates), so replacing it changes what the runner PULLS,
    never what it may reach.

    EXCEPT `projects`, which the runner now REPORTS on every heartbeat (spec
    2026-07-28). Accepting a hand-written value would be accepting a ghost edit:
    the next heartbeat overwrites it seconds later, so the caller sees a 200,
    believes the repo is declared, and dispatches into a hole. A 422 naming the
    real fix is the honest answer. `projects` is also PRESERVED across a write that
    omits it — it belongs to the runner now, so a capabilities PATCH must not drop
    it as a side effect.
    """
    runner = _runner_or_404(request, runner_id)
    if "projects" in payload.capabilities:
        raise HttpError(
            422,
            "`projects` is reported by the runner, not set by hand — it is replaced "
            "on every heartbeat from what the box actually has. To make a repo "
            "routable, open it as a project in emdash on that runner (or set "
            "RUNNER_PROJECTS on a cloud runner). PATCH `agents`/`sessions` freely.",
        )
    reported = runner.capabilities.get("projects")
    caps = dict(payload.capabilities)
    if reported is not None:
        caps["projects"] = reported
    runner.capabilities = caps
    runner.save(update_fields=["capabilities"])
    return runner


@router.post("/runners/{runner_id}/retire", response={204: None})
def retire_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Retire a runner — a decommission, not a liveness state (see
    Runner.live_status). Idempotent by construction: _runner_or_404 already excludes
    retired runners, so retiring an already-retired runner 404s at lookup rather than
    no-opping here. Reversible via /unretire — but see the caveat below.

    Deletes the runner's RunnerAssignment rows in the same transaction. A
    retired runner is invisible to _runner_visibility_q, but its stale
    assignment rows were NOT — GET /agents/{slug}/runners kept listing them,
    and PUT /agents/{slug}/runners round-trips that same list to save any
    unrelated change, so a lingering row 422'd every matrix save with "unknown
    or retired runner id" (a prod incident 2026-07-25). Ranks of the
    survivors need not be compacted — RunnerAssignment.rank is only ever
    compared relatively (0 = first choice), never assumed contiguous.

    CAVEAT: /unretire brings the runner back but does NOT restore these deleted
    assignments — re-add it to the agents that should route to it via the
    matrix (PUT /agents/{slug}/runners). Restoring the runner's identity keeps
    its bindings/credentials; its routing membership is intentionally not
    resurrected, since the fleet may have moved on while it was retired."""
    runner = _runner_or_404(request, runner_id)
    with transaction.atomic():
        runner.status = Runner.RETIRED
        runner.save(update_fields=["status"])
        RunnerAssignment.objects.filter(runner=runner).delete()
    return Status(204, None)


@router.post("/runners/{runner_id}/unretire", response=RunnerOut)
def unretire_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Bring a retired runner back, keeping its identity — and therefore every
    RunnerBinding, assignment and session that points at it.

    Retirement used to be a ONE-WAY DOOR, which made it a trap rather than a
    decision. `_runner_or_404` 404s a retired runner, so its daemon's heartbeat,
    claim and session-report calls all fail forever once retired; and `pair_runner`
    unconditionally CREATES a row, so the only recovery — re-pairing — minted a new
    id and orphaned the old one's bindings. Retiring a laptop you were logged out of
    therefore silently destroyed its sessions' identity the moment you brought it
    back (labs 2026-07-25: jj-mbp-cdp, 10 sessions).

    Restores DISCONNECTED, not ONLINE: liveness is observed, never asserted — the
    next heartbeat is what makes it online (Runner.live_status). Idempotent for an
    already-live runner.
    """
    runner = _runner_or_404(request, runner_id, include_retired=True)
    if runner.status == Runner.RETIRED:
        runner.status = Runner.DISCONNECTED
        runner.save(update_fields=["status"])
    return runner


@router.post("/runners/{runner_id}/pause", response=RunnerOut)
def pause_runner(request: HttpRequest, runner_id: uuid.UUID, payload: PauseIn):
    """Stop ROUTING work to this runner, without decommissioning it.

    The remote half of the runner's local `~/.canopy/PAUSED` sentinel, which only
    a human on that machine could ever drop. Jonathan runs the fleet under two
    macOS accounts for token-limit failover (Runner.host), and moving work off a
    rate-limited account means silencing its runner FROM THE OTHER ONE — impossible
    until now, because ~/.canopy there is owned by the other account. The only
    reachable lever was `retire`, which is a decommission: it deletes
    RunnerAssignment rows `unretire` does not restore, and it 404s the daemon's own
    heartbeat and claim calls. This destroys nothing and is reversible by
    construction.

    ENFORCED SERVER-SIDE, so it does not depend on the runner cooperating or even
    being up to date: `live_status` reports PAUSED, and `claim_next_turn`'s first
    guard already refuses anything that is not ONLINE. A paused runner may poll as
    often as it likes and will simply never be handed a turn. That also means a
    pause takes effect against an OLD runner binary with no deploy on that box.

    It outranks a PIN. `claim_next_turn` returns before pin matching, deliberately:
    a pin is operator intent, but so is a pause, and it is the more specific and
    more recent one. Letting a pin resurrect a parked box would re-open the exact
    hole this closes — work landing on an account that must not spend tokens. A
    turn pinned to a paused runner stays QUEUED (queued turns never expire) and
    lands when it comes back.

    Pause stops STARTING work, never finishing it: an executing turn keeps its
    lease and reports completion normally, matching the local sentinel's behavior.

    Idempotent — pausing an already-paused runner refreshes the note and returns
    200 rather than erroring, so a retry after a dropped response is safe.
    """
    runner = _runner_or_404(request, runner_id)
    if not runner.paused:
        runner.paused_at = timezone.now()
    runner.paused = True
    runner.paused_note = (payload.note or "")[:200]
    runner.save(update_fields=["paused", "paused_note", "paused_at"])
    return runner


@router.post("/runners/{runner_id}/unpause", response=RunnerOut)
def unpause_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Resume routing to a paused runner. The exact inverse of /pause — it clears
    the flag and nothing else, because /pause destroyed nothing to restore.

    Contrast `unretire`, which cannot undo its own side effects (the deleted
    assignment rows) and says so. That asymmetry is the whole argument for pause
    existing as its own verb rather than people reaching for retire.

    Does NOT assert liveness: the runner comes back to whatever its heartbeat says
    it is, exactly as `unretire` restores DISCONNECTED rather than ONLINE. Liveness
    is observed, never asserted. Idempotent on an already-running runner.
    """
    runner = _runner_or_404(request, runner_id)
    if runner.paused:
        runner.paused = False
        runner.paused_note = ""
        runner.paused_at = None
        runner.save(update_fields=["paused", "paused_note", "paused_at"])
    return runner


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
        code_version=payload.code_version,
        code_sha=payload.code_sha,
        code_committed_at=payload.code_committed_at,
        projects=payload.projects,
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
    from apps.canopy_sessions.models import RunnerBinding
    from apps.realtime import groups

    runner = _runner_or_404(request, runner_id)
    binding = (
        RunnerBinding.objects.select_related("session")
        .filter(session_id=payload.session_id, runner=runner).first()
    )
    if binding is None:
        raise HttpError(404, "session not bound to this runner")
    if chat_services.transcript_sourced(binding.session):
        # Not "was this session discovered in emdash?" — where a conversation
        # started says nothing about where its record belongs. A phone-created chat
        # is driven by the same runner, in the same emdash session, writing the same
        # transcript, so it persists the same way (see services.transcript_sourced).
        #
        # Ordinal-less events (an old runner) stay live-view-only: persisting
        # assistant rows without the user side would blank the tail fallback's
        # human half the moment any row exists.
        # The payload IS the row's content (structured fields + "text"), stored
        # verbatim — a tool_use's {id,name,input} and a tool_result's
        # {tool_use_id,is_error} are what the client pairs and renders on, so
        # flattening to text here would strip exactly the half that makes a tool
        # call legible.
        chat_services.persist_transcript_rows(binding.session, [
            {"index": e.index, "role": e.kind,
             "text": (e.payload or {}).get("text", ""), "content": e.payload or {}}
            for e in payload.events if e.index >= 0
        ])
    sgroup = groups.session_group(payload.session_id)
    n = 0
    from apps.canopy_sessions.transcript_noise import is_system_noise

    for e in payload.events:
        # The noise filter has to run HERE too, not just in
        # persist_transcript_rows. Now that user events fan out live, a filter
        # applied only on the durable path would drop a harness marker from
        # history while still pushing it to every watching client — so it would
        # appear live, then vanish on reload. Same rule, both paths.
        if e.kind.startswith("activity:"):
            # Fans out, never persists — index -1 already excludes it from the
            # durable write, and it has no transcript row to be.
            groups.publish(sgroup, {
                "type": "chat.turn_event",
                "event": {"kind": e.kind, "seq": e.seq, "payload": e.payload},
                "turn_id": None,
            })
            n += 1
            continue
        if e.kind == "user" and is_system_noise((e.payload or {}).get("text", "")):
            n += 1
            continue
        # User events ARE fanned out. They used to be withheld on the grounds
        # that "the sender's client already echoed them optimistically" — true
        # for text typed in the web, false for text typed directly into emdash,
        # which no web client ever saw. The result was that typing in emdash and
        # watching on the phone silently dropped your own words until a reload
        # (observed 2026-07-27). The client upserts on turn_index, so a message
        # that does arrive twice collapses instead of doubling.
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

    pinned = None
    if payload.runner_id is not None:
        # The question here is exactly "can the caller SEE this runner?" — a runner
        # it cannot see must 422 as unknown, never be attachable because its UUID
        # was guessed. So this follows the READ predicate: pinning directs work at
        # a box, it does not speak for it, and any member can already enqueue a
        # turn this runner will claim (claim_next_turn re-checks the tenant either
        # way). Retired runners are excluded too — pinning to one strands the turn
        # forever, since nothing can claim it.
        pinned = (
            Runner.objects.exclude(status=Runner.RETIRED)
            .filter(_runner_read_q(request))
            .filter(id=payload.runner_id)
            .first()
        )
        if pinned is None:
            raise HttpError(422, f"unknown or retired runner id: {payload.runner_id}")

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
        pinned_runner=pinned,
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
        # Resolve the TARGET before filtering. The tenant filter below would
        # otherwise express a permission denial as an empty list — 200 [] — which
        # is indistinguishable from "this agent has never run", while the sibling
        # route /api/agents/<slug>/tasks/ answers the same denial with 404.
        #
        # That divergence is load-bearing, not cosmetic: agents legitimately hold
        # different permission sets, so a fleet survey routinely asks about agents
        # it cannot see, and every one of them read back as healthy-and-idle. Ada's
        # `conduct` reads this endpoint per agent to spot stuck turns.
        # (`agent_health` is incidentally safe — it resolves /api/agents/<slug>/
        # first, which already 404s.)
        #
        # 404 rather than 403, and the same 404 for a typo, so the endpoint cannot
        # be used to enumerate which tenants' agents exist (see _agent_or_404).
        _agent_or_404(request, agent)
        qs = qs.filter(agent__slug=agent)
    if status:
        qs = qs.filter(status__in=status.split(","))
    # Tenant filter, split by target kind (agent / project / session) — mirrors
    # claim_next_turn's tenant_q (services.py) and _turn_or_404 (this module).
    #
    # Security review 2026-07-26, hole B: this used to be one clause,
    # `Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True)`,
    # with two independent problems. (1) The isnull leg left an unhomed
    # agent's turns (TurnOut: prompt, origin_ref, session_id) visible to ANY
    # authenticated caller — more permissive than `_agent_or_404`'s fail-closed
    # gate, recreating the exact list-vs-gate drift `_runner_visibility_q`'s
    # docstring warns about (a turn the list shows, then 404s on every
    # action). (2) `agent__workspace_id` traverses a nullable FK: for a
    # PROJECT or SESSION turn (agent_id IS NULL), the LEFT JOIN makes
    # `agent__workspace_id__isnull=True` true unconditionally — so that one
    # clause also leaked every tenant's project/session turns to every
    # authenticated user, regardless of the turn's own workspace. (Confirmed
    # empirically pre-fix: an unrelated stranger's GET returned another
    # tenant's project turn.) Both close by gating each target kind on its
    # own workspace source, with no null-workspace escape hatch anywhere in
    # this list — unlike claim_next_turn, which keeps one for agent turns
    # specifically as a documented, claim-routing-only exception.
    qs = qs.filter(
        (Q(agent__isnull=False) & Q(agent__workspace_id__in=slugs))
        | (Q(agent__isnull=True) & Q(chat_session__isnull=True) & Q(workspace_id__in=slugs))
        | (Q(chat_session__isnull=False) & Q(chat_session__workspace_id__in=slugs))
    )
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


@router.post("/turns/{turn_id}/transcript", response=TranscriptAppendOut)
def append_turn_transcript(request: HttpRequest, turn_id: uuid.UUID, payload: TranscriptAppendIn):
    """Ingest a batch of raw `claude -p` JSONL lines onto a turn's retained
    transcript. Same tenancy gate as every other turn route (_turn_or_404) —
    deliberately not a bespoke check; a transcript is more sensitive than a
    turn's status, and a second gate is exactly how the session-turn tenancy
    leak happened.

    Appending to an already-terminal turn is allowed by design: a runner may
    flush its last batch after finishing (services.append_transcript has no
    status check either).

    `batch_id`, if given, dedups a retry of the immediately-preceding batch
    (F5). The per-turn size ceiling (F2) is enforced inside
    `services.append_transcript` itself, never here — crossing it drops the
    batch's content and writes a marker rather than 4xx-ing, because a
    turn's transcript getting long is not a reason to fail a live run;
    `truncated` in the response tells the caller that happened.
    """
    turn = _turn_or_404(request, turn_id)
    total_bytes = sum(len(line.encode("utf-8")) for line in payload.lines)
    if total_bytes > TRANSCRIPT_APPEND_MAX_BYTES:
        raise HttpError(
            422,
            f"transcript batch too large ({total_bytes} bytes; "
            f"limit is {TRANSCRIPT_APPEND_MAX_BYTES} bytes per request)",
        )
    transcript = services.append_transcript(turn, payload.lines, batch_id=payload.batch_id)
    return {
        "line_count": transcript.line_count,
        "bytes_raw": transcript.bytes_raw,
        "truncated": transcript.truncated,
    }


@router.get("/turns/{turn_id}/transcript", summary="Raw retained JSONL for a turn")
def read_turn_transcript(request: HttpRequest, turn_id: uuid.UUID):
    """The byte-for-byte raw transcript, streamed as plain JSONL bytes — a
    turn with nothing ever appended reads as an empty 200, not a 404;
    absence of a transcript is not absence of a turn. No `response=` schema
    is declared so Ninja returns this StreamingHttpResponse verbatim instead
    of trying to serialize it (mirrors apps/canopy_sessions.api's plain
    HttpResponse for attachment_content).

    Streams `services.iter_transcript`, which inflates the stored gzip
    INCREMENTALLY in bounded chunks rather than decompressing the whole blob
    into memory at once (security review 2026-07-26, F3 — the sibling
    `/events` route caps at 500 rows for the same underlying reason).

    A PRIOR version of this fix instead served the still-gzipped bytes
    directly with `Content-Encoding: gzip`, betting the HTTP client would
    inflate transparently — a follow-up review empirically falsified that:
    `curl --compressed` and `httpx` both return only the FIRST gzip member
    of Task 1's multi-member on-disk format, silently truncating the
    transcript with a 200 and no error, and this repo's own runner client
    (`runner/canopy_runner`, `urllib.request`) does no content-decoding at
    all — it would have treated raw gzip bytes as JSONL. Streaming plaintext
    here removes that wire-format gamble: every caller gets exactly the
    bytes `services.read_transcript` would return, with none of its
    all-at-once memory cost.
    """
    turn = _turn_or_404(request, turn_id)
    return StreamingHttpResponse(
        services.iter_transcript(turn), content_type="application/x-ndjson"
    )


@router.post("/turns/{turn_id}/start", response=TurnOut)
def start_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnStartIn):
    turn = _turn_or_404(request, turn_id)
    if turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        raise ProblemError(409, "Turn not startable", detail=f"status={turn.status}")
    return services.mark_running(turn, session_id=payload.session_id)


@router.post("/turns/{turn_id}/finish", response=TurnOut)
def finish_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnFinishIn):
    turn = _turn_or_404(request, turn_id)
    if payload.status not in (Turn.DONE, Turn.FAILED, Turn.CANCELLED):
        raise HttpError(422, "finish status must be done|failed|cancelled")
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

    claim_next_turn DERIVES FROM THE SAME TWO FUNCTIONS this does —
    services.runner_tenant_slugs and services.agent_tenant_q — so the two rules
    AGREE BY CONSTRUCTION: every schedule this runner may fire produces a turn
    that same runner may claim. They are no longer two hand-written predicates
    that happen to match; there is one predicate with two callers.

    That matters because they briefly diverged, and the divergence was an
    outage, not a nicety. claim_next_turn shipped scoped to the Runner.workspace
    FK while this predicate derived from paired_by — so a runner homed to
    `alpha` whose pairer also belongs to `beta` could SEE and FIRE beta's
    schedules here but could not CLAIM the resulting turns, leaving them QUEUED
    forever. Because one laptop runner serves a fleet that deliberately spans
    workspaces, that stopped 4 of 5 production agents from executing at all. The
    resolution was to converge the CLAIM onto paired_by (this predicate's rule),
    NOT to narrow this one onto the FK: the FK records where a runner lives, not
    who it may work for. tests/test_claim_schedule_parity.py fails if the two
    ever disagree again.

    NULL paired_by fails closed inside runner_tenant_slugs (empty slug set →
    `__in=set()` matches nothing), which is stricter than _runner_visibility_q's
    legacy-ungated allowance — an orphaned runner can be operated, but can never
    sync or fire a schedule. That used to be a separate `.none()` branch here; it
    was folded into the shared helper so there is one mechanism, not two that
    can drift.
    """
    qs = AgentSchedule.objects.filter(enabled=True).select_related("agent")
    return qs.filter(services.agent_tenant_q(services.runner_tenant_slugs(runner)))


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
