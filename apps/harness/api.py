"""Django Ninja router for /api/harness — runner registry + turn lifecycle."""
from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Q
from django.http import HttpRequest, StreamingHttpResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404
from ninja import Router, Status
from canopy_sdk import contract
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import ProblemError
from apps.api.pagination import Page, clamp_limit, paginate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import initiator as who
from . import services
from . import turn_mode as turn_modes
from .models import AgentSchedule, Runner, RunnerAssignment, RunnerDrill, Turn, WorkspaceRunnerOrder
from .schedule_services import serialize_schedule
from .schemas import (
    CallerContextOut,
    ClaimedTurnOut,
    BackfillSyncOut,
    BackfillWriteOut,
    CloseSyncOut,
    MenuAnswerResultIn,
    MenuAnswerResultOut,
    MenuAnswerSyncOut,
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
    RetireOut,
    RunnerAdminIn,
    RunnerAdminOut,
    RunnerFlagsIn,
    RunnerEngineIn,
    RunnerCredentialStatusOut,
    RunnerMintClaimOut,
    RunnerMintCodeIn,
    RunnerMintOut,
    RunnerMintResultIn,
    RunnerMintStartIn,
    RunnerMintUrlIn,
    RunnerDrillOut,
    RunnerGitHubReadinessOut,
    TurnGitHubTokenOut,
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
    TurnMessagesOut,
    TurnOut,
    TurnStartIn,
)

router = Router(auth=session_auth, tags=["harness"])

# Allowed values for TurnEvent.kind. Kept in sync with the event kinds the
# runner/agent side actually emits; anything else 422s at the API boundary
# rather than being silently persisted.
# Session-stream kinds that are STATE, not transcript: they fan out to watching
# clients and are never persisted (index -1 already excludes them from the durable
# write, and they have no transcript row to be).
#
#   activity:  is the agent producing right now — working | idle | blocked
#   stop:      did a stop the human asked for actually land — requested | stopped
#              | failed. A SEPARATE axis from activity on purpose: after a stop
#              that did not take, the agent is still `working`, and that is true
#              and must stay true. Folding "the stop failed" into activity would
#              either lie about what the agent is doing or lose the stop entirely.
LIVE_ONLY_PREFIXES = ("activity:", "stop:")

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
    same 404 as a missing agent (no existence leak). A domain user who has not
    been let into the agent's workspace (an invite, or an approved access
    request) is a non-member and gets exactly that 404 — nothing joins
    automatically.

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
    ws = getattr(request, "workspace_slug", None)
    if ws and agent.workspace_id != ws:
        raise HttpError(404, f"agent '{slug}' not found")  # wrong tenant
    if not agent.workspace_id or not wsvc.is_member(request.user, agent.workspace_id):
        raise HttpError(404, f"agent '{slug}' not found")
    return agent


def _agent_for_write_or_404(request: HttpRequest, slug: str) -> Agent:
    """An agent whose EXECUTION the caller may drive. Editor or owner.

    Harness-local twin of `apps.agents.api._agent_for_write`, duplicated for
    the same reason `_agent_or_404` is: api modules must not depend on each
    other, and the harness is framework-tier. Both read the role through the
    one shared reader (`wsvc.member_role`), so the duplication is of the CALL,
    not of what "editor" means.

    Enqueuing a turn is the executing half of the author tier — it is arbitrary
    prompt text run as the agent, holding the agent's resolved credentials, and
    bare membership let a `viewer` do it. Resolve-then-authorize: a non-member
    gets `_agent_or_404`'s 404 and never a 403.
    """
    agent = _agent_or_404(request, slug)
    if not perms.can(request.user, agent.workspace_id, perms.AGENT_WORK):
        raise HttpError(403, "running a turn for this agent requires the editor or owner role")
    return agent


def _runner_owned_q(request: HttpRequest) -> Q:
    """Ownership: the caller owns it. A runner NOBODY owns is acted on by
    nobody — it used to be acted on by everyone (heartbeat, claim, retire, and
    its plaintext credential bundle), the NULL-means-allow shape this repo has
    removed everywhere else. Every live runner on labs has a owner (2026-10-02)."""
    return Q(owner=request.user)


def _runner_read_q(request: HttpRequest) -> Q:
    """'Runners this caller can SEE' — derived from the TENANT, like claim time.

    Seeing a runner and acting on one are different questions that one predicate
    answered, and its `owner == caller` leg is right for the second and wrong
    for the first. A workspace's fleet is typically owned by ONE human, so every
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
    `owner=user` made every stuck turn read as `config` for anyone who had not
    personally paired a runner. Same rule, second call site.

    What is deliberately NOT inherited from the act-on predicate: its
    `workspace_id__isnull=True` leg. There it is backstopped by ownership, so it
    means "your own legacy runner"; here, with ownership gone, the same leg would
    mean "everyone's", which is the NULL-means-allow shape this codebase has
    already paid to remove from six tenancy predicates (PRs #378, #421, #423). A
    runner with no workspace has no tenant to share, so it stays visible only to
    its owner — hence the `& _runner_owned_q` on that leg alone.

    Nothing listed is secret to a member: RunnerOut carries status, capabilities,
    host and `owner_email` — never credentials, which have their own
    owner-gated route.
    """
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
    `owner` is what `claim_next_turn` derives a tenant from, so acting as
    someone else's runner is acting with their memberships.

    `_runner_read_q` is deliberately WIDER, which gives up the invariant these two
    used to share by being one function ("never list a runner every action then
    404s on"). That invariant is now carried explicitly instead of structurally,
    by `RunnerOut.can_manage`: a client can say "runner X serves this repo, ask
    its owner to declare on it" rather than discovering ownership from a bare 404
    on an action it was told to try. Read ⊇ act-on holds by construction — every
    leg here appears in the read, ANDed with less.
    """
    ws = getattr(request, "workspace_slug", None)
    if ws:
        # This workspace or one above it. A division's agents run on boxes that
        # live in its parent (the fleet's do: they live in `dimagi`, the agents in
        # `connect`), so an exact match refused the caller's OWN box on every
        # tenant-scoped routing save — the agent's Settings page and the fleet
        # map — while the flat route took it. Ownership still applies below; this
        # widens only where the caller's own box may live, never whose it is.
        from apps.workspaces.models import Workspace

        here = Workspace.objects.filter(slug=ws).first()
        wq = Q(workspace_id__in=[ws, *(here.ancestor_slugs() if here else [])])
    else:
        wq = Q(workspace_id__in=wsvc.user_workspace_slugs(request.user)) | Q(workspace_id__isnull=True)
    return wq & _runner_owned_q(request)


def _runner_admin_or_404(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    """Resolve a runner this caller may ADMINISTER — a wider tier than acting AS
    it, and deliberately not the same predicate.

    Reached from the operator-facing routes only: setting credentials, the
    browser sign-in, and STARTING a readiness drill (owner decision 2026-10-04:
    "let admins of a runner also run readiness checks"). Everything that speaks
    FOR the runner — heartbeat, claim, executing a drill's turns, the runner's
    own credential fetch and its half of a mint — keeps `_runner_or_404`,
    because those act with the owner's memberships.

    Starts from what the caller can SEE (the tenant), then requires the explicit
    grant on top. Both legs matter: the tenant leg stops a grant in one workspace
    reaching a runner in another, and the grant leg stops the 22 auto-joined
    members of a `dimagi.com` workspace inheriting its credentials.
    """
    runner = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(_runner_read_q(request))
        .filter(pk=runner_id)
        .first()
    )
    # 404 rather than 403, matching _runner_or_404: the harness must not leak
    # which runners exist to someone who may not act on them.
    if runner is None or not services.can_administer_runner(request.user, runner):
        raise HttpError(404, "runner not found")
    return runner


def _runner_or_404(
    request: HttpRequest, runner_id: uuid.UUID, *, include_retired: bool = False
) -> Runner:
    """Resolve a live runner via _runner_visibility_q — the same predicate
    list_runners filters on, so a runner that is listed is always one you can
    act on. Binding to runner.owner (not to a specific token) is
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
    """`_tenant_turn_or_404`, then `site ∩ user`: a connected site acting for its
    visitor sees only its own agents' turns (apps/tokens/delegation.py). Same
    uniform 404, so a site cannot learn another agent's turn exists."""
    from apps.tokens import delegation

    turn = _tenant_turn_or_404(request, turn_id)
    offered = delegation.offered_for(request)
    if offered is not None and delegation.turn_agent_id(turn) not in offered:
        raise HttpError(404, "turn not found")
    return turn


def _agents_own_thread_q():
    """Session turns on an AGENT's own thread (an email or Slack thread: a runner-
    origin session nobody created), which every member of its workspace may see
    ran — the same interaction tier as an agent turn. Before #734 these were agent
    turns and the whole tenant listed them; converting them to session turns hid
    them from everyone but the agent's admins, and a member's routing audit read
    the silence as "0 turns" (#1087). Their CONTENT stays gated by the session
    (`turn_access.redact`), and a person's own chat is not one of these."""
    from apps.canopy_sessions.models import Session

    return Q(chat_session__origin=Session.ORIGIN_RUNNER, chat_session__created_by__isnull=True,
             chat_session__agent__isnull=False)


def _visible_sessions(request: HttpRequest):
    """Chat sessions the caller may read — the chat ACL, as a subquery, for
    every harness listing that would otherwise show a session turn's prompt or
    a session's messages to the whole tenant."""
    from apps.canopy_sessions import access as session_access
    from apps.canopy_sessions.models import Session

    return Session.objects.filter(session_access.visible_session_q(request.user)).values("pk")


def _turn_content_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """A turn whose CONTENT (ledger, transcript, caller context) the caller may
    read (`turn_access.can_read_turn_content`) — a log, the admin's unless you
    started or run it. Same uniform 404 as a turn you cannot see at all."""
    from . import turn_access

    turn = _turn_or_404(request, turn_id)
    if not turn_access.can_read_turn_content(request.user, turn):
        raise HttpError(404, "turn not found")
    return turn


def _reporting_turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """A turn the caller may REPORT on — start, finish, append ledger events or
    transcript lines. That is the runner protocol, so it belongs to the box that
    claimed the turn: the caller must be its owner.

    `_turn_or_404` alone (membership) let any viewer fail someone else's live
    turn, inject `question`/`approval` events, or append fake transcript lines.
    An UNCLAIMED turn has no box yet; reporting on one is a write to the work
    itself, so it takes the same tier as cancelling it (editor on an agent turn,
    write access to the chat on a session turn). Same uniform 404 otherwise.
    """
    turn = _turn_or_404(request, turn_id)
    if turn.claimed_by_id is not None:
        if turn.claimed_by.owner_id != request.user.pk:
            raise HttpError(404, "turn not found")
        return turn
    if turn.agent_id:
        if not perms.can(request.user, turn.agent.workspace_id, perms.AGENT_WORK):
            raise HttpError(404, "turn not found")
    elif turn.chat_session_id:
        from apps.canopy_sessions import access as session_access

        if not session_access.can_write(request.user, turn.chat_session):
            raise HttpError(404, "turn not found")
    return turn


def _site_turn_q(request: HttpRequest):
    """The turns an acting site may see, as a Q — or None when no site acts."""
    from apps.tokens import delegation

    offered = delegation.offered_for(request)
    if offered is None:
        return None
    return Q(agent_id__in=offered) | Q(agent__isnull=True, chat_session__agent_id__in=offered)


def _tenant_turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
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
    ws = getattr(request, "workspace_slug", None)
    if turn.chat_session_id:
        from apps.canopy_sessions import access as session_access

        slug = turn.chat_session.workspace_id
        if (ws and slug != ws) or not wsvc.is_member(request.user, slug):
            raise HttpError(404, "turn not found")
        # And the CHAT's own ACL: a session turn's prompt and transcript are
        # that conversation. Tenant membership alone let any co-tenant read a
        # private web chat here that canopy_sessions/access.py hides from them.
        if not session_access.can_read(request.user, turn.chat_session):
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
    # A runner is OWNED by a person. `owner` is the box's identity for life —
    # its claims run with the owner's memberships and only the owner may grant
    # administration — so pairing with an agent's token makes a box nobody owns:
    # the human at the keyboard can't manage it, and their own work on it is
    # attributed to the agent. Measured 2026-10-02 (sarveshtewari-mbp-cdp, paired
    # as ace@dimagi-ai.com). An agent may still ADMINISTER a box through a grant.
    agent = getattr(request.user, "agent_identity", None)
    if agent is not None:
        raise HttpError(
            403,
            f"{request.user.email} is the login of agent '{agent.slug}', and a runner must "
            "be owned by a person — pair with YOUR canopy-web token (the "
            "canopy:canopy-web-pat-mint skill writes ~/.claude/canopy/workbench-token); "
            "the agent can then be granted admin via POST /api/harness/runners/{id}/admins",
        )
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
        # derives from owner), but every session report 404s, so its
        # sessions silently never surface (prod incident 2026-07-25: a
        # multi-workspace owner made user_default_workspace() None and the
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
        # `profiles` is REPORTED on heartbeat, never declared: a box that says at
        # pairing it can confine a caller's session would be believed until its
        # first beat. Dropped here rather than refused, so an old pairing script
        # that copies a whole capabilities dict still pairs.
        capabilities={k: v for k, v in payload.capabilities.items()
                      if k not in ("profiles", "envelope", "midturn")},
        host=payload.host,
        owner=request.user,
        workspace_id=ws_slug,
    )
    return Status(201, runner)


@router.post("/runners/{runner_id}/credential", response=RunnerCredentialStatusOut,
             summary="Set a cloud runner's credential bundle (owner only)")
def set_runner_credential(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCredentialIn):
    """Store the per-runner secrets a cloud runner fetches at startup — its Claude
    login (plus the secondary subscription and API key it fails over to).
    Owner-gated exactly
    like heartbeat/claim (owner == caller). Non-clobbering per field. Encrypted
    at rest; the response is masked (booleans, never values)."""
    runner = _runner_admin_or_404(request, runner_id)
    services.set_runner_credential(
        runner,
        claude_token=payload.claude_token,
        claude_token_secondary=payload.claude_token_secondary,
        claude_api_key=payload.claude_api_key,
        claude_token_label=payload.claude_token_label,
        claude_token_secondary_label=payload.claude_token_secondary_label,
        updated_by=request.user,
    )
    return services.runner_credential_status(runner)


@router.post("/runners/{runner_id}/credential/swap", response=RunnerCredentialStatusOut,
             summary="Swap the primary and fallback Claude logins")
def swap_runner_logins(request: HttpRequest, runner_id: uuid.UUID):
    """The fallback becomes the primary and the primary the fallback — each
    login keeps its name. The runner reads the new order the next time it
    re-reads its bundle."""
    runner = _runner_admin_or_404(request, runner_id)
    services.swap_runner_logins(runner, updated_by=request.user)
    return services.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential/status", response=RunnerCredentialStatusOut,
            summary="Which credential slots are set (masked — booleans, never values)")
def get_runner_credential_status(request: HttpRequest, runner_id: uuid.UUID):
    """The operator's read: which slots are filled, without putting a secret on a
    screen. Separate from the GET below, which returns REAL VALUES for the runner
    to consume.

    Exists because the only way to read this used to be a no-op POST — and that
    WROTE, bumping `updated_at`/`updated_by` and destroying the one signal that
    says when a credential was last actually rotated."""
    runner = _runner_admin_or_404(request, runner_id)
    return services.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential", response=RunnerCredentialOut,
            summary="Fetch this runner's credential bundle (the runner, via its PAT)")
def get_runner_credential(request: HttpRequest, runner_id: uuid.UUID) -> RunnerCredentialOut:
    """A cloud runner fetches its own secrets to stage into its environment. Returns
    the actual token values over HTTPS, gated to the runner's owner (owner ==
    caller) — the same trust boundary that lets that caller claim turns as the
    runner. Laptop/emdash runners never call this (they use ambient auth)."""
    runner = _runner_or_404(request, runner_id)
    return RunnerCredentialOut(**services.get_runner_credential(runner))


@router.post("/runners/{runner_id}/turns/{turn_id}/github-token",
             response=TurnGitHubTokenOut,
             summary="One claimed turn's GitHub credential (its agent owner's)")
def turn_github_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID):
    """The GitHub token and git identity for ONE turn this runner is executing:
    the turn's agent owner's token for that agent. canopy decides whose — the
    runner only names the turn. Refused (409, with the reason) when the owner
    has lent none or it has expired; there is no shared fallback."""
    from apps.agents import delegations

    runner = _runner_or_404(request, runner_id)
    # Only a turn this runner holds right now. A finished turn, or one another
    # box claimed, gets the same 404 as one that does not exist.
    turn = (
        Turn.objects.select_related("agent__owner", "chat_session__agent__owner",
                                    "initiator_user", "initiator_contact")
        .filter(pk=turn_id, claimed_by=runner, status__in=services.EXECUTING)
        .first()
    )
    if turn is None:
        raise HttpError(404, "turn not found")
    from apps.agents.services import runner_may_hold_agent

    # A claim already requires this; re-asked here because a owner can be
    # demoted mid-turn, and this is the owner's GitHub identity.
    agent = delegations.turn_agent(turn)
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "turn not found")
    try:
        issued = delegations.github_token_for_turn(turn)
    except delegations.DelegationError as exc:
        raise ProblemError(409, "No GitHub identity for this turn", detail=str(exc)) from exc
    agent = delegations.turn_agent(turn)
    try:
        from apps.events import services as events

        events.record(
            [{
                "source": "agents.delegations",
                "kind": "agent.github.issued",
                "level": "info",
                "key": f"{agent.slug}:{turn.pk}",
                "summary": f"{agent.slug}: GitHub token issued to {runner.name} for one turn",
                "payload": {"agent": agent.slug, "turn": str(turn.pk),
                            "runner": str(runner.pk), "as": issued["github_login"]},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a turn its token
        pass
    return issued


@router.get("/runners/{runner_id}/github-readiness",
            response=list[RunnerGitHubReadinessOut],
            summary="Can each agent this runner serves open a pull request?")
def runner_github_readiness(request: HttpRequest, runner_id: uuid.UUID):
    """Checked live against GitHub, per agent routed to this runner. A box calls
    this when it boots so a missing, expired or under-scoped token is a health
    check going red, not a 403 in the middle of a turn."""
    from apps.agents import delegations

    runner = _runner_or_404(request, runner_id)
    out = []
    for agent in services.agents_served_by(runner):
        delegations.check_github(agent)
        status, detail = delegations.readiness(agent)
        st = delegations.status(agent)
        out.append({"agent_slug": agent.slug, "status": status, "detail": detail,
                    "login": st.get("login", ""), "expires_at": st.get("expires_at")})
    return out


@router.post("/runners/{runner_id}/mint", response=RunnerMintOut,
             summary="Ask a runner to start a browser sign-in")
def start_runner_mint(request: HttpRequest, runner_id: uuid.UUID,
                      payload: RunnerMintStartIn | None = None):
    """Begin re-authenticating this runner's Claude subscription from a browser.

    The runner picks this up on its next poll, runs the real `claude setup-token`
    under a pty, and posts back the URL a human must open. canopy-web is only the
    relay: it never holds the PKCE verifier and is never the OAuth client.

    Supersedes any unfinished mint rather than refusing — a stalled sign-in (a
    closed tab, a runner restart mid-flow) must not block every later attempt.

    `slot` names which login the new token replaces — `primary` (the default)
    or `secondary`, the fallback subscription.
    """
    runner = _runner_admin_or_404(request, runner_id)
    return services.start_runner_mint(runner, requested_by=request.user,
                                      slot=payload.slot if payload else "primary")


@router.get("/runners/{runner_id}/mint", response=RunnerMintOut | None,
            summary="The current sign-in attempt, if any")
def get_runner_mint(request: HttpRequest, runner_id: uuid.UUID):
    """What the waiting human's screen renders: whether the URL is up yet, and
    how the attempt ended. Null when no sign-in has ever been started."""
    runner = _runner_admin_or_404(request, runner_id)
    return services.current_runner_mint(runner)


@router.get("/runners/{runner_id}/mint/claim", response=RunnerMintClaimOut,
            summary="Work the RUNNER owes on a sign-in (polled)")
def claim_runner_mint(request: HttpRequest, runner_id: uuid.UUID):
    """Polled on the runner's existing tick, and polled rather than pushed for
    the same reason as `menu-answers`: a control frame published while the
    runner's WS channel is down reaches a group with no consumer and is silently
    dropped, while the runner keeps heartbeating and reads ONLINE throughout.

    Returns a row only when the runner owes work — asked to start, or handed a
    code. While the ball is with the human (`awaiting_code`) this stays null, or
    the runner would restart the CLI under a URL somebody is already using.

    Reading a code CONSUMES it: an authorization code is spent on first use, so a
    second delivery could only fail, and a used code left in the row would be a
    credential nobody is accounting for.
    """
    runner = _runner_or_404(request, runner_id)
    mint = services.claim_runner_mint(runner)
    if mint is None:
        return {"mint": None, "code": ""}
    return {"mint": mint, "code": services.take_mint_code(mint)}


@router.post("/runners/{runner_id}/mint/url", response=RunnerMintOut,
             summary="The runner reports the URL a human must open")
def post_runner_mint_url(request: HttpRequest, runner_id: uuid.UUID,
                         payload: RunnerMintUrlIn):
    runner = _runner_or_404(request, runner_id)
    mint = services.current_runner_mint(runner)
    if mint is None or mint.status in mint.FINISHED:
        raise HttpError(409, "no sign-in is in progress for this runner")
    return services.record_mint_url(mint, payload.url)


@router.post("/runners/{runner_id}/mint/code", response=RunnerMintOut,
             summary="A human submits the authorization code")
def post_runner_mint_code(request: HttpRequest, runner_id: uuid.UUID,
                          payload: RunnerMintCodeIn):
    """The one secret a human handles in this flow, and it is single-use."""
    runner = _runner_admin_or_404(request, runner_id)
    mint = services.current_runner_mint(runner)
    if mint is None or mint.status != mint.AWAITING_CODE:
        raise HttpError(409, "this runner is not waiting for a code")
    if not payload.code.strip():
        raise HttpError(422, "the authorization code is empty")
    return services.submit_mint_code(mint, payload.code)


@router.post("/runners/{runner_id}/mint/result", response=RunnerMintOut,
             summary="The runner reports the outcome (and delivers the token)")
def post_runner_mint_result(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerMintResultIn):
    """The minted token arrives HERE, never in the browser — it goes straight
    into the encrypted credential bundle, so the only secret that ever reaches a
    human's screen is the single-use authorization code."""
    runner = _runner_or_404(request, runner_id)
    mint = services.current_runner_mint(runner)
    if mint is None:
        raise HttpError(409, "no sign-in is in progress for this runner")
    return services.finish_runner_mint(mint, token=payload.token, detail=payload.detail)


def _admin_row(a) -> dict:
    return {"user_id": a.user_id, "email": a.user.email,
            "granted_by_email": a.granted_by.email if a.granted_by else "",
            "created_at": a.created_at}


@router.get("/runners/{runner_id}/admins", response=list[RunnerAdminOut],
            summary="Who may administer this runner")
def list_runner_admins(request: HttpRequest, runner_id: uuid.UUID):
    """Visible to anyone who can already administer the box — the answer to
    "who else can fix this", which is the question a stuck box raises."""
    runner = _runner_admin_or_404(request, runner_id)
    return [_admin_row(a) for a in services.list_runner_admins(runner)]


@router.post("/runners/{runner_id}/admins", response=RunnerAdminOut,
             summary="Grant someone administration of this runner (owner only)")
def grant_runner_admin(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerAdminIn):
    """Granting stays with the OWNER, not with grantees.

    Deliberate: an administrator can change what the box runs on, but letting
    them mint more administrators makes the grant self-propagating, and then the
    explicit list stops being a list of people the owner actually trusted.
    """
    runner = _runner_or_404(request, runner_id)
    user = User.objects.filter(email__iexact=payload.email.strip()).first()
    if user is None:
        # No leak either way: the caller already owns this runner, and "no such
        # account" is the only useful thing to say about a typo'd address.
        raise HttpError(404, f"no account with email {payload.email!r}")
    # Same tenant leg the admin resolver enforces, checked here so the failure
    # is a clear 422 at grant time rather than a mystifying 404 the first time
    # the grantee tries to use it.
    if runner.workspace_id and not wsvc.is_member(user, runner.workspace_id):
        raise HttpError(
            422,
            f"{user.email} is not a member of the workspace this runner belongs to",
        )
    return _admin_row(services.grant_runner_admin(runner, user, granted_by=request.user))


@router.delete("/runners/{runner_id}/admins/{user_id}", response={204: None},
               summary="Revoke administration (owner only)")
def revoke_runner_admin(request: HttpRequest, runner_id: uuid.UUID, user_id: int):
    runner = _runner_or_404(request, runner_id)
    user = User.objects.filter(pk=user_id).first()
    if user is None or not services.revoke_runner_admin(runner, user):
        raise HttpError(404, "no such grant on this runner")
    return Status(204, None)


@router.put("/runners/{runner_id}/flags", response=RunnerOut,
            summary="Declare what this runner's owner vouches for")
def set_runner_flags(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerFlagsIn):
    """Replace the runner's declared flags. `zdr`: this box uses only
    zero-data-retention keys for Claude. canopy cannot check a declaration; it
    records who made it. A host may require a flag of every conversation its
    visitors hold, and those conversations then run only on runners declaring it.
    """
    runner = _runner_admin_or_404(request, runner_id)
    try:
        wanted = set(contract.parse_runner_requirements(payload.flags))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    added, removed = services.set_runner_flags(runner, wanted, by=request.user)
    if runner.workspace_id and (added or removed):
        from apps.events import services as events

        events.record([
            {"source": "harness.runners", "kind": kind, "level": "info",
             "summary": f"{request.user.email} {verb} {flag} on {runner.name}",
             "payload": {"runner": str(runner.pk), "flag": flag, "by": request.user.email}}
            for kind, verb, names in (("runner.flag_declared", "declared", added),
                                      ("runner.flag_withdrawn", "withdrew", removed))
            for flag in sorted(names)
        ], workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    # Per (caller, runner), as list_runners stamps them: RunnerOut defaults both
    # to True, so an unstamped reply would show an admin who is not the owner
    # controls that then 404.
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = services.can_administer_runner(request.user, out)
    return out


@router.put("/runners/{runner_id}/engine", response=RunnerOut,
            summary="Choose the session runtime a laptop runner opens new sessions in")
def set_runner_engine(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerEngineIn):
    """Set the runtime (`emdash` or `claude-desktop`) a laptop runner opens new
    sessions in. Takes effect on the runner's next heartbeat; sessions already
    running stay where they are."""
    # canopy-web#1188. The runner reads `engine` off its heartbeat response, so a
    # flip needs no restart and no shell on the box, and is safe while sessions are
    # live: a thread already running in one runtime keeps its own session, and
    # routing is the same either way. Owner or runner admins, like flags — it
    # changes how the box works, not whose memberships it speaks with. A cloud
    # runner has one runtime (Claude headless) and refuses rather than ignoring it.
    runner = _runner_admin_or_404(request, runner_id)
    if payload.engine not in dict(Runner.ENGINE_CHOICES):
        raise HttpError(422, f"unknown runtime '{payload.engine}' — one of: "
                             + ", ".join(dict(Runner.ENGINE_CHOICES)))
    if runner.kind == Runner.CLOUD:
        raise HttpError(422, "a cloud runner runs Claude headless; it has no session runtime to choose")
    if runner.engine != payload.engine:
        before = runner.engine
        runner.engine = payload.engine
        runner.save(update_fields=["engine"])
        if runner.workspace_id:
            from apps.events import services as events

            events.record([{
                "source": "harness.runners", "kind": "runner.engine_changed", "level": "info",
                "summary": f"{request.user.email} switched {runner.name} from {before} to {payload.engine}",
                "payload": {"runner": str(runner.pk), "from": before, "to": payload.engine,
                            "by": request.user.email},
            }], workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    out.can_manage = out.owner_id in (request.user.id, None)
    out.can_administer = services.can_administer_runner(request.user, out)
    return out


@router.get("/runners/", response=list[RunnerOut], summary="List the fleet I can see")
def list_runners(request: HttpRequest):
    """The supervisor's runner status, and the fleet read every preflight makes.

    Scoped by TENANT (`_runner_read_q`), not by who owns what: a member who
    paired nothing used to list nothing, which reads identically to "this
    workspace has no runners" and is the wrong answer to draw a conclusion from.
    Each row carries `can_manage` for the ownership half. Retired runners are
    excluded at lookup, as everywhere else.
    """
    qs = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(_runner_read_q(request))
        .prefetch_related("drills", "declared_flags")
        .order_by(models.F("last_heartbeat_at").desc(nulls_last=True))
    )
    from apps.tokens import delegation

    offered = delegation.offered_for(request)
    if offered is not None:
        # A site sees only the runners that serve its own agents — enough for a
        # "continue on…" picker, and nothing about the rest of the fleet.
        following = {r.pk for r in qs if services.agents_following_runner(r) & set(offered)}
        qs = qs.filter(Q(agent_assignments__agent_id__in=offered) | Q(pk__in=following)).distinct()
    rows = list(qs[:50])
    for r in rows:
        # Resolved here rather than in the schema because it is a property of the
        # (caller, runner) PAIR, and a Ninja resolver only sees the row.
        r.can_manage = r.owner_id in (request.user.id, None)
        # Administration is a WIDER tier than acting as the runner, so it gets
        # its own flag rather than overloading can_manage — the credentials block
        # and the drill panel are gated by different routes.
        r.can_administer = services.can_administer_runner(request.user, r)
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
    for key in ("profiles", "envelope", "midturn"):
        if key in payload.capabilities:
            # Reported, like `projects` — and here it is a SECURITY property: a hand
            # edit claiming a runner can confine a caller's session would route
            # confined turns to a box that runs them in the full profile.
            raise HttpError(422, f"`{key}` is reported by the runner on every heartbeat, "
                                 "not set by hand.")
    caps = dict(payload.capabilities)
    for key in ("projects", "profiles", "envelope", "midturn"):
        if runner.capabilities.get(key) is not None:
            caps[key] = runner.capabilities[key]
    runner.capabilities = caps
    runner.save(update_fields=["capabilities"])
    return runner


@router.post("/runners/{runner_id}/retire", response=RetireOut)
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
        rows = list(RunnerAssignment.objects.filter(runner=runner).select_related("agent")
                    .order_by("agent__slug", "source", "actor"))
        RunnerAssignment.objects.filter(runner=runner).delete()
        # And out of every workspace's default order. Left in, it reads as a
        # listed runner the order's next save then 422s on — the 2026-07-25
        # incident, one level up — and every agent following that order would
        # silently lose the box.
        orders = [row.workspace for row in WorkspaceRunnerOrder.objects.filter(runner=runner)
                  .select_related("workspace")]
        WorkspaceRunnerOrder.objects.filter(runner=runner).delete()
    dropped = [{"agent": r.agent.slug, "source": r.source, "actor": r.actor} for r in rows]
    if orders:
        from apps.events import services as events

        for ws in orders:
            events.record([{
                "source": "harness.runners", "kind": "runner.route_dropped", "level": "warning",
                "summary": (f"{request.user.email} retired {runner.name}: it is no longer in "
                            f"{ws.slug}'s default runner order"),
                "payload": {"runner": str(runner.pk), "workspace_order": ws.slug},
            }], workspace=ws)
    if dropped and runner.workspace_id:
        from apps.events import services as events

        events.record([
            {"source": "harness.runners", "kind": "runner.route_dropped", "level": "warning",
             "summary": (f"{request.user.email} retired {runner.name}: {d['agent']} "
                         f"{d['source'] or 'default order'}"
                         + (f" for {d['actor']}" if d["actor"] else "")
                         + " no longer routes there"),
             "payload": {"runner": str(runner.pk), **d}}
            for d in dropped
        ], workspace=runner.workspace)
    return {"runner": runner.name, "dropped_routes": dropped}


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
        profiles=payload.profiles,
        envelope=payload.envelope,
        midturn=payload.midturn,
        health=payload.health.model_dump() if payload.health is not None else None,
        mailboxes_readable=payload.mailboxes_readable,
    )


@router.post("/runners/{runner_id}/refresh", response=RunnerOut)
def refresh_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Ask this runner to refresh itself at its next idle moment: re-run its
    bootstrap, which updates the canopy plugin and CLI, Claude Code, and each
    agent's provisioning, then restart. `refresh_pending` stays true until the
    runner reports a bootstrap newer than the request."""
    # The administer tier, not the act-as tier: this changes what the box runs
    # on, the same class of operation as setting its credentials.
    runner = _runner_admin_or_404(request, runner_id)
    return services.request_refresh(runner)


@router.post("/runners/{runner_id}/claim", response={200: ClaimedTurnOut, 204: None})
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
    # The same credentials the WebSocket claim carries — see `claiming.py`,
    # which exists because the two channels once disagreed about them.
    from .claiming import issue_credentials

    return Status(200, issue_credentials(turn))


def _project_workspace_or_404(request: HttpRequest, ws_slug: str):
    """Tenant-gate a project session's workspace, mirroring _agent_or_404 for the
    agent case. A project link has no agent to derive tenancy from, so without
    this any runner could read another user's rolling `summary` by guessing
    thread_key.

    The workspace is passed EXPLICITLY (from the turn the runner is executing, via
    TurnOut.workspace_slug), not derived from a default: the owner may belong to
    several workspaces, and a project turn already carries the one it belongs to.
    The owner must be a member of it. Same 404-not-403 rule: a non-member gets
    404, never a disclosure that the workspace exists.
    """
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
        try:
            services.record_session(
                None, payload.thread_key, runner=runner, project=payload.project, workspace=ws,
                session_key=payload.session_key, session_id=payload.session_id,
                agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
                title=payload.title,
            )
        except services.ThreadSessionNotFound:
            raise HttpError(404, "session not found")
        return services.resolve_session(
            None, payload.thread_key, runner, project=payload.project, workspace=ws
        )
    agent = _agent_or_404(request, payload.agent_slug)
    title = payload.title
    if payload.turn_id:
        # The server names the session from the turn when it knows it: the email's
        # subject, the schedule's name — never the raw command a cloud turn's
        # prompt often is ("/ace:turn --thread …", 2026-10-03).
        turn = (Turn.objects.select_related("agent", "chat_session__agent")
                .filter(pk=payload.turn_id, claimed_by=runner).first())
        if turn is not None:
            title = services.turn_session_title(turn, payload.title)
    try:
        services.record_session(
            agent, payload.thread_key, runner=runner,
            session_key=payload.session_key, session_id=payload.session_id,
            agent_task_ext_id=payload.agent_task_ext_id, summary=payload.summary,
            title=title,
        )
    except services.ThreadSessionNotFound:
        # A session id that is not this agent's, or sits in a tenant the runner's
        # owner is not in: refused as if it did not exist, and nothing rebound.
        raise HttpError(404, "session not found")
    if payload.turn_id and payload.session_key:
        services.stamp_turn_session(payload.turn_id, runner, payload.session_key)
    return services.resolve_session(agent, payload.thread_key, runner)


@router.post("/runners/{runner_id}/sessions", response=SessionReportOut)
def report_sessions(request: HttpRequest, runner_id: uuid.UUID, payload: ReportSessionsIn):
    """The runner reports the open emdash sessions it can see. Wholesale per runner.
    Owner-gated via _runner_or_404 (404, not 403). Sessions are tenant-owned; they
    default to the runner's workspace (dimagi in practice), which the owner is a
    member of by construction."""
    runner = _runner_or_404(request, runner_id)
    ws = runner.workspace
    if ws is None:
        raise HttpError(404, "runner has no workspace")
    count = services.replace_reported_sessions(
        runner, ws, payload.sessions, payload.archived, complete=payload.complete
    )
    return SessionReportOut(count=count)


@router.get("/runners/{runner_id}/streams", response=StreamSyncOut)
def list_streams(request: HttpRequest, runner_id: uuid.UUID):
    """Every session this runner backs, with what the server already holds of each.

    NOT just the watched ones any more, and that is the point. While this filtered
    on `stream_desired=True`, transcript rows only ever reached the server for a
    session someone happened to have open, and only from the moment they opened it
    — so the durable record was a side effect of being looked at. Measured on labs
    (2026-07-31) across 12 live sessions: the server held 983 of 6119 rows (16%),
    with 8 sessions at exactly zero. "Load full session" existed to paper over that
    by asking the runner at read time, which is why it was both slow and unreliable.

    `live` is the old `stream_desired` — it now decides only whether rows FAN OUT
    to watching clients, never whether they are persisted. Persisting is
    unconditional because the transcript is the durable source (spec 2026-07-24)
    and building the rows costs the runner ~29 ms for a 6.5 MB file; there was
    never a cost argument for keeping it, only the accident that live-view and
    durability shared one flag.

    `first_index`/`last_index` bound what the server holds, so the runner can tell
    "ship only what is new" from "ship the history I am missing" — a Max alone can
    only ever append above the high-water mark and so can never fill a hole below
    it, which is how a session ended up stuck at 8.6% with no way to self-heal."""
    from django.db.models import F, Max as _Max, Min as _Min

    from apps.canopy_sessions.models import RunnerBinding, Session

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, session__status=Session.ACTIVE)
        .exclude(session_key="")
        # ACTIVE only. Widening this from `stream_desired` to "every session"
        # would otherwise sweep in every session the box has ever held — labs
        # accumulated 71 at one point — and re-ship each one's full history on
        # every runner restart, for conversations that are retired and whose
        # transcripts are not growing. An archived session is not abandoned,
        # though: `drain_backfills` is a separate path this filter does not
        # touch, so an explicit "Load full session" on one still works. Eager for
        # live sessions, on demand for retired ones.
        # Scoped to the CURRENT EPOCH, and reported in the RUNNER's own ordinal
        # space (see RunnerBinding.index_offset). Both halves matter for a
        # transferred session, and for opposite reasons:
        #   - the filter, because rows inherited from the previous box are not in
        #     this runner's transcript at all. An unfiltered Min/Max would hand it
        #     a high-water mark it can never reach, so every record of the live
        #     conversation would sit BELOW the marker and never ship — issue #615's
        #     failure exactly, reintroduced by preserving history rather than by
        #     reusing a task name.
        #   - the subtraction, because the runner compares these against ordinals
        #     it computes from its OWN file. It needs no knowledge of the offset:
        #     the shift is applied on write (persist_transcript_rows) and undone
        #     here, so both directions stay inside the server.
        # Offset 0 (never transferred) makes the filter cover everything and the
        # subtraction a no-op, i.e. the previous behaviour byte for byte.
        .annotate(
            _first_index=_Min(
                "session__messages__turn_index",
                filter=Q(session__messages__turn_index__gte=F("index_offset")),
            ),
            _last_index=_Max(
                "session__messages__turn_index",
                filter=Q(session__messages__turn_index__gte=F("index_offset")),
            ),
        )
    )

    def _local(marker, offset):
        """A server-space marker in the runner's own ordinal space. None stays
        None — "I hold nothing of this transcript", which is what a freshly
        transferred session is, and it tells the runner to ship from the top."""
        return None if marker is None else max(int(marker) - int(offset or 0), 0)

    return {"streams": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         # emdash_project, never `project`: an agent chat leaves `project` blank
         # and its worktree lives under the agent's own repo. Sending "" here is
         # what stopped agent sessions ever being streamed or backfilled.
         "project": b.session.emdash_project,
         "last_index": _local(b._last_index, b.index_offset),
         "first_index": _local(b._first_index, b.index_offset),
         "live": b.stream_desired,
         # What the markers above are markers INTO. They are per-file ordinals, so
         # a runner reading a different transcript must discard them rather than
         # resume against them (issue #615).
         "transcript_id": b.transcript_id}
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
    # transcript ordinal (e.index) -> the author persist_transcript_rows found
    # for it, if any — carried into the live frame below so a watcher sees who
    # sent an unmarked line without waiting for a reload (stream_map's
    # kind=="user" branch reads this when its own marker-parse finds none).
    authors_by_index: dict[int, dict] = {}
    if chat_services.transcript_sourced(binding.session):
        # BEFORE any write: if these ordinals index a different transcript than the
        # rows already held, those rows are a different conversation's and would
        # interleave with (or silently swallow) this one. Dropping them is safe
        # because a mismatch is exactly when the runner ships the full history.
        # Unless a closed session's NAME was reused, in which case these rows are a
        # new conversation's and go to a new session, leaving the old one whole.
        if chat_services.fork_if_name_reused(binding.session, payload.transcript_id):
            binding = RunnerBinding.objects.select_related("session").get(pk=binding.pk)
        chat_services.ensure_transcript_identity(binding.session, payload.transcript_id)
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
        created = chat_services.persist_transcript_rows(binding.session, [
            {"index": e.index, "role": e.kind,
             "text": (e.payload or {}).get("text", ""), "content": e.payload or {}}
            for e in payload.events if e.index >= 0
        ])
        from apps.canopy_sessions.authorship import parse as parse_marker

        # A user row's text is the prompt AS DELIVERED — marker line included —
        # and the receivers compare it with the bare Turn.prompt (Slack's
        # "carrying on elsewhere" check), so they get the words without it.
        streamed = [(e.index, e.kind,
                     parse_marker(text)[1] if e.kind == "user" else text)
                    for e in payload.events
                    if e.index >= 0 and e.kind in ("user", "assistant")
                    for text in [str((e.payload or {}).get("text") or "")]
                    if text]
        if created and streamed:
            from apps.harness.signals import transcript_rows_streamed

            session = binding.session
            transaction.on_commit(lambda: transcript_rows_streamed.send(
                sender=type(session), session=session, rows=streamed))
        user_indices = [
            e.index + binding.index_offset
            for e in payload.events if e.index >= 0 and e.kind == "user"
        ]
        if user_indices:
            from apps.canopy_sessions.models import Message

            authors_by_index = {
                turn_index - binding.index_offset: author
                for turn_index, author in Message.objects.filter(
                    session=binding.session, turn_index__in=user_indices,
                ).values_list("turn_index", "author")
                if author
            }
    if not binding.stream_desired:
        # Persisted above, but nobody is watching, so there is nothing to push.
        # The runner now tails EVERY session it backs so the durable record stops
        # depending on someone having the chat open (see list_streams); this is the
        # gate that keeps that from also broadcasting every session in the fleet to
        # session groups no client has joined.
        return {"count": 0}
    sgroup = groups.session_group(binding.session_id)
    n = 0
    from apps.canopy_sessions.transcript_noise import is_system_noise

    for e in payload.events:
        # The noise filter has to run HERE too, not just in
        # persist_transcript_rows. Now that user events fan out live, a filter
        # applied only on the durable path would drop a harness marker from
        # history while still pushing it to every watching client — so it would
        # appear live, then vanish on reload. Same rule, both paths.
        if e.kind.startswith(LIVE_ONLY_PREFIXES):
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
        event_payload = e.payload
        if e.kind == "user":
            author = authors_by_index.get(e.index)
            if author:
                event_payload = {**(e.payload or {}), "author": author}
        groups.publish(sgroup, {
            "type": "chat.turn_event",
            "event": {"kind": e.kind, "seq": e.seq, "payload": event_payload},
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
         "project": b.session.emdash_project}
        for b in bindings
    ]}


@router.get("/runners/{runner_id}/closes", response=CloseSyncOut)
def list_closes(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions this runner has been asked to close and has not closed yet.

    The twin of `/menu-answers`: `close_session` relays a WS frame, and a frame
    published while the control channel is down is discarded silently while the
    API answers `ok:true`. Drained on the poll tick, so a lost frame costs a tick
    rather than leaving the emdash task open and the session active forever.
    """
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner, close_requested=True)
        .exclude(session_key="")
    )
    return {"closes": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.emdash_project}
        for b in bindings
    ]}


@router.get("/runners/{runner_id}/menu-answers", response=MenuAnswerSyncOut)
def list_menu_answers(request: HttpRequest, runner_id: uuid.UUID):
    """Answers a human has given that this runner has not pressed yet.

    The WS control frame is the fast path; this is the one that makes the answer
    SURVIVE. A frame published while the runner's control channel is down goes to
    a Channels group with no consumer and is discarded — and because the runner
    still heartbeats over REST it reads ONLINE the whole time, so the API answers
    `ok:true` and nothing records the loss. Measured on labs 2026-08-01: an answer
    sent at 10:50 never reached the runner, between reconnects at 10:16 and 10:58.

    Drained on the poll tick the runner already runs, same as `/backfills`.
    """
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    bindings = (
        RunnerBinding.objects.select_related("session")
        .filter(runner=runner)
        .exclude(pending_answer__isnull=True)
        .exclude(session_key="")
    )
    return {"answers": [
        {"session_id": str(b.session_id), "session_key": b.session_key,
         "project": b.session.emdash_project,
         "answer_id": (b.pending_answer or {}).get("id") or "",
         "option": (b.pending_answer or {}).get("option"),
         "selections": (b.pending_answer or {}).get("selections"),
         "texts": (b.pending_answer or {}).get("texts")}
        for b in bindings
    ]}


@router.post("/runners/{runner_id}/menu-answer-result", response=MenuAnswerResultOut)
def post_menu_answer_result(request: HttpRequest, runner_id: uuid.UUID,
                            payload: MenuAnswerResultIn):
    """The runner reports what became of an answer, which retires it.

    Matched on `answer_id`: applying an answer twice means a SECOND keystroke into
    a session that has already moved on, so a result for an answer that has since
    been replaced must NOT clear the newer one.
    """
    from apps.canopy_sessions.models import RunnerBinding

    runner = _runner_or_404(request, runner_id)
    binding = RunnerBinding.objects.filter(
        session_id=payload.session_id, runner=runner).first()
    if binding is None:
        return {"ok": False}
    current = (binding.pending_answer or {}).get("id")
    if not current or current != payload.answer_id:
        return {"ok": False}
    binding.pending_answer = None
    binding.save(update_fields=["pending_answer"])
    return {"ok": True}


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
    session = chat_services.fork_if_name_reused(session, payload.transcript_id) or session
    # Same guard as the live path: history from a different transcript replaces
    # what is held rather than merging into it. Idempotent across chunks — the
    # first one records the id, so the rest of the ship matches and writes through.
    chat_services.ensure_transcript_identity(session, payload.transcript_id)
    written = chat_services.write_backfill(session, [m.dict() for m in payload.messages])
    # Only the LAST chunk clears the ask. A transcript is shipped in byte-budgeted
    # chunks (see SessionBackfillIn.final), and clearing on the first one would
    # retire the request while most of the history was still in flight — a ship
    # that then died would leave a permanently partial session with nothing left
    # to re-trigger it.
    if payload.final and binding.backfill_requested:
        binding.backfill_requested = False
        binding.save(update_fields=["backfill_requested", "updated_at"])
    return {"written": written}


@router.get("/turns/unclaimable", response=list[UnclaimableTurnOut],
            summary="Queued turns no online runner can claim")
def list_unclaimable_turns(request: HttpRequest):
    """A queued turn addressed to an agent/repo nothing declares sits forever with
    no signal (one sat 12h). Surfacing it turns a silent stall into a warning."""
    site_q = _site_turn_q(request)
    if site_q is not None:
        return services.unclaimable_queued_turns(request.user, turn_q=site_q)
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
        # Editor, not bare membership: this enqueues arbitrary prompt text to be
        # executed AS the agent with the agent's credentials, which is the
        # author/executor tier by the role ladder's own definition. Project and
        # session turns are deliberately NOT gated the same way — a session turn
        # is what a chat send produces, and talking to an agent is exactly what
        # the interaction tier is for.
        agent = _agent_for_write_or_404(request, payload.agent_slug)
    else:
        # A project turn carries its own tenant.
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
        # A project turn is a prompt run in a repo on somebody's box — the same
        # author tier as an agent turn. It was bare membership, so a viewer
        # could send an arbitrary prompt to run in any repo the fleet declares.
        if not perms.can(request.user, workspace, perms.AGENT_WORK):
            raise HttpError(403, "running a turn requires the editor role or above")

    if payload.origin == Turn.ORIGIN_EMAIL:
        # An email turn names its OWN asker from `origin_ref` (the sender and our
        # receiver's Authentication-Results), and a DMARC-aligned sender who is a
        # member becomes a VERIFIED user initiator — so whoever may post one may
        # write that verdict. The one legitimate poster is the runner that read
        # the mailbox, authenticated as its owner, and a box holds an agent only
        # when its owner is one of the agent's admins (runner_may_hold_agent). An
        # editor who is not an admin posting "dmarc=pass" from the owner's address
        # would otherwise be promoted to the owner. A project turn has no mailbox.
        if agent is None or not agent.is_admin(request.user):
            raise HttpError(
                403,
                "an email turn is posted by the runner that read the agent's mailbox, "
                "whose owner is one of the agent's admins; send other work with "
                "origin=api",
            )

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
        if agent is None and payload.project not in pinned.project_names():
            # claim_next_turn's pin arm skips target matching, so without this a
            # pinned repo prompt lands on a box that never declared the repo —
            # routing's one rule for a project turn (runner_target_q's
            # `project__in`) is that the box drives that repo. Unpinned project
            # turns route by declaration as before.
            raise HttpError(
                403,
                f"runner {pinned.name} does not declare the repo {payload.project!r}; "
                "pin a runner that does, or dispatch without a runner",
            )
        if agent is not None:
            from apps.agents.services import runner_may_hold_agent

            # claim_next_turn refuses it anyway; say so now rather than leave a
            # turn queued that nothing will ever claim.
            if not runner_may_hold_agent(pinned, agent):
                raise HttpError(
                    403,
                    f"runner {pinned.name} cannot run {agent.slug}: its owner must "
                    "be the agent's owner, a workspace owner, or one of its admins",
                )
            from apps.agents import access

            # Pinning places the agent's work on one box: the agent's admins may
            # pin any box that can hold it, anyone else only a box they administer
            # (docs/architecture/access.md).
            if not access.may_pin_runner(request.user, agent, pinned):
                raise HttpError(
                    403,
                    f"pinning {agent.slug}'s turn to runner {pinned.name} is for the agent's "
                    "owner or admins, or for someone who administers that runner; dispatch "
                    "without a runner and the agent's routing places it",
                )

    initiator = (None if payload.origin == Turn.ORIGIN_EMAIL
                 else who.for_request(request, via=payload.origin))
    if agent is not None and initiator is not None:
        _check_access(request, payload, agent, initiator)
    _check_requested_turn_mode(request, payload, agent, initiator)

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
        # The CALLER is the asker for every postable origin except email, which a
        # runner posts on a stranger's behalf — enqueue_turn names that sender
        # itself. Passing the caller for email would record the runner's owner as
        # the person who wrote in.
        initiator=initiator,
        requested_turn_mode=payload.turn_mode or "",
        requested_turn_mode_by=request.user if payload.turn_mode else None,
        # What the caller was running inside (overrides X-Canopy-Parent-*).
        parent=payload.parent,
    )
    return Status(201 if created else 200, turn)


#: Credentials that establish the person on THIS request — a signed-in browser
#: or their own token (an MCP OAuth token is one). Not a widget's delegated
#: token: an embedding host vouching for its visitor must not unlock `auto`.
_TURN_MODE_VERIFIED = frozenset({who.SESSION, who.PAT})


def _check_requested_turn_mode(request, payload: TurnIn, agent, initiator) -> None:
    """Who may ask for a turn's mode (TurnIn.turn_mode). Raises 422/403, else returns.

    Mirrors apps/harness/turn_mode.py: `manual` from anyone who may enqueue the
    turn at all (the editor gate above already ran), `auto` only from the agent's
    owner or an admin — `Agent.is_admin`, which counts workspace owners — on a
    verified credential. The claim re-checks the admin leg (`turn_mode.requested`).
    """
    mode = payload.turn_mode
    if not mode:
        return
    if agent is None:
        raise HttpError(422, "turn_mode applies only to an agent turn (agent_slug), not a project")
    if payload.origin == Turn.ORIGIN_EMAIL:
        # A runner posts email on a stranger's behalf; the caller here is the
        # runner's owner, not the person whose message the turn answers.
        raise HttpError(422, "turn_mode cannot be requested on an email turn")
    if mode == turn_modes.MANUAL:
        return
    verified = (initiator is not None and initiator.kind == who.USER
                and initiator.assurance in _TURN_MODE_VERIFIED)
    if not verified:
        raise HttpError(
            403, "turn_mode=auto needs a signed-in session or your own personal access token")
    from apps.agents import access

    if not access.decide(agent, request.user, verified=verified,
                         origin=payload.origin).may_request_auto:
        raise HttpError(
            403,
            f"turn_mode=auto is for {agent.slug}'s owner or admins; you may request "
            "turn_mode=manual (or none — a workspace editor's turns run manual anyway), "
            "or ask its owner to make you an admin",
        )


def _check_access(request, payload: TurnIn, agent, initiator) -> None:
    """Refuse at enqueue a turn the runner would refuse, saying what the caller
    CAN do — THE rule (`apps.agents.access.decide`), asked of the requester.

    The editor gate above already ran, so a person reaching this is a workspace
    editor or above and is refused only by a future tightening; it is asked here
    anyway so this door can never disagree with the others."""
    from apps.agents import access

    user = request.user if getattr(request.user, "is_authenticated", False) else None
    d = access.decide(agent, user, verified=initiator.kind == who.USER
                      and initiator.assurance in _TURN_MODE_VERIFIED, origin=payload.origin)
    if d.access == access.NONE:
        raise HttpError(403, d.reason or f"{agent.slug} does not take work from you")


def visible_turns_qs(request: HttpRequest, *, all_memberships: bool = False):
    """Every turn this caller may see LISTED — the exact tenant + site filter
    `list_turns` applies, newest first. Shared so a derived view over turns (the
    huddles API) can never show a turn `/api/harness/turns/` would not. Content
    is a separate gate: rows still go through `turn_access` before their prompt
    or transcript is read.

    `all_memberships` ignores a `/api/w/{ws}/` pin and spans every workspace the
    caller belongs to — for a view whose subject itself crosses workspaces (a
    huddle's members can live in several). It never widens past the caller's own
    memberships: that is exactly the unpinned `/api/harness/turns/` scope."""
    ws = None if all_memberships else getattr(request, "workspace_slug", None)
    slugs = {ws} if ws else wsvc.user_workspace_slugs(request.user)
    qs = Turn.objects.select_related(
        "agent", "claimed_by", "initiator_user", "initiator_contact",
        "pinned_runner", "requested_turn_mode_by",
        # A chat turn's session and its agent are read per row by TurnOut.
        "chat_session", "chat_session__agent",
    ).order_by("-created_at")
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
        | (Q(chat_session__isnull=False) & Q(chat_session__workspace_id__in=slugs)
           & (Q(chat_session__in=_visible_sessions(request)) | _agents_own_thread_q()))
    )
    site_q = _site_turn_q(request)
    if site_q is not None:
        qs = qs.filter(site_q)
    return qs


@router.get("/turns/", response=list[TurnOut])
def list_turns(
    request: HttpRequest,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
    huddle: str | None = None,
    parent_turn: uuid.UUID | None = None,
):
    qs = visible_turns_qs(request)
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
        target = _agent_or_404(request, agent)
        # An email or Slack thread targets a SESSION, not the agent (enqueue_turn
        # converts it — the XOR check constraint allows only one), so the agent
        # sits on `chat_session.agent`. Filtering on `agent` alone dropped every
        # one of them: `?agent=ace` listed no ACE email turn after 2026-09-10, for
        # every caller, and hal's routing audit read that as "0 turns" (#1087).
        qs = qs.filter(Q(agent=target) | Q(agent__isnull=True, chat_session__agent=target))
    if status:
        qs = qs.filter(status__in=status.split(","))
    # A huddle's turns (canopy `huddle`): its anchor and every round carry
    # `origin_ref.huddle`; a round's `parent_turn` is the anchor.
    if huddle:
        qs = qs.filter(origin_ref__huddle=huddle)
    if parent_turn:
        qs = qs.filter(parent_turn_id=parent_turn)
    limit = max(1, min(limit, 200))  # clamp; default 100 keeps existing callers unchanged
    from . import turn_access

    # Everyone sees THAT a turn ran; its prompt is a log (turn_access).
    return turn_access.redact(list(qs[:limit]), request.user)  # filter BEFORE slicing


@router.get("/sessions", response=list[EmdashSessionOut])
def list_sessions(request: HttpRequest):
    """Open emdash sessions the caller can see — across their workspaces, live runners
    only, newest-first. Drives the phone's Open Sessions list."""
    from apps.agents.models import Agent
    from apps.tokens import delegation

    rows = services.list_visible_sessions(request.user)
    offered = delegation.offered_for(request)
    if offered is None:
        return rows
    slugs = set(Agent.objects.filter(pk__in=offered).values_list("slug", flat=True))
    return [r for r in rows if r.agent in slugs]


@router.get("/turns/{turn_id}", response=TurnOut)
def get_turn(request: HttpRequest, turn_id: uuid.UUID):
    from . import turn_access

    return turn_access.redact([_turn_or_404(request, turn_id)], request.user)[0]


@router.get("/turns/{turn_id}/caller-context", response=CallerContextOut,
            summary="Who asked for this turn, and what canopy knows about them")
def get_turn_caller_context(request: HttpRequest, turn_id: uuid.UUID):
    """The caller envelope for one turn: the asker, how sure canopy is (for THIS
    message), their relationship to the agent, and the contact profile canopy
    holds. The same document the claiming runner receives."""
    # Same gate as every turn route. The contact profile is no wider than
    # /api/contacts/, which any member of the tenant can already read.
    from .caller_context import build

    return {"envelope": build(_turn_content_or_404(request, turn_id))}


@router.post("/turns/{turn_id}/events", response=TurnEventCountOut)
def append_turn_events(request: HttpRequest, turn_id: uuid.UUID, payload: TurnEventsIn):
    turn = _reporting_turn_or_404(request, turn_id)
    for event in payload.events:
        if event.kind not in ALLOWED_EVENT_KINDS:
            raise HttpError(422, f"unknown event kind '{event.kind}'")
    count = services.append_events(turn, [e.dict() for e in payload.events])
    return {"count": count}


@router.get("/turns/{turn_id}/events", response=TurnEventsOut)
def read_turn_events(request: HttpRequest, turn_id: uuid.UUID, after: int = 0):
    turn = _turn_content_or_404(request, turn_id)
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
    turn = _reporting_turn_or_404(request, turn_id)
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


@router.get(
    "/turns/{turn_id}/messages", response=TurnMessagesOut,
    summary="A turn's transcript as readable messages",
)
def read_turn_messages(request: HttpRequest, turn_id: uuid.UUID):
    """The turn's retained transcript parsed into messages (user, assistant,
    tool use, tool result), with secrets scrubbed. Bounded: `truncated` is true
    when the view stopped early. Empty for a turn that kept no transcript."""
    # Same gate as the raw route below — a transcript is a LOG
    # (turn_access.can_read_turn_content). Parsing is bounded
    # (services.TRANSCRIPT_VIEW_MAX_MESSAGES) and reads the blob incrementally.
    turn = _turn_content_or_404(request, turn_id)
    messages, truncated = services.transcript_messages(turn)
    return {"messages": messages, "truncated": truncated}


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
    turn = _turn_content_or_404(request, turn_id)
    return StreamingHttpResponse(
        services.iter_transcript(turn), content_type="application/x-ndjson"
    )


@router.post("/turns/{turn_id}/start", response=TurnOut)
def start_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnStartIn):
    turn = _reporting_turn_or_404(request, turn_id)
    if turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        raise ProblemError(409, "Turn not startable", detail=f"status={turn.status}")
    return services.mark_running(turn, session_id=payload.session_id)


@router.post("/turns/{turn_id}/finish", response=TurnOut)
def finish_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnFinishIn):
    turn = _reporting_turn_or_404(request, turn_id)
    if payload.status not in (Turn.DONE, Turn.FAILED, Turn.CANCELLED):
        raise HttpError(422, "finish status must be done|failed|cancelled")
    # Record the session BEFORE the terminal check: a re-reported finish is otherwise
    # a no-op, and this is the only moment the runner tells us which emdash session
    # the turn drove. Never blank an existing value — the first report wins.
    if payload.session_key and not turn.session_key:
        turn.session_key = payload.session_key[:200]
        turn.save(update_fields=["session_key"])
    if turn.status in Turn.TERMINAL:
        return turn  # idempotent finish
    # Read the state BEFORE finishing: a non-terminal result is now ambiguous. It
    # means either "you tried to finish a turn you never claimed" (the 409 this guard
    # was written for) or "your sessionless failure was put back on the queue"
    # (services.finish_turn's requeue — a SUCCESSFUL finish that legitimately lands on
    # QUEUED). Only the executing case can produce the second, so that is what tells
    # them apart; keying on the result status alone would 409 every retry.
    was_executing = turn.status in services.EXECUTING
    result = services.finish_turn(turn, status=payload.status, result_note=payload.result_note)
    if result.status not in Turn.TERMINAL and not was_executing:
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
    # Same tier as the enqueue that produced it: an AGENT turn is the author
    # tier, so a viewer who cannot dispatch one may not withdraw one either.
    #
    # The session carve-out is STRUCTURAL rather than a condition spelled out
    # here — `turn_targets_agent_xor_project_xor_session` makes a session turn
    # carry `agent=NULL` and derive its agent through `chat_session`, so this
    # branch cannot see one. That is the right outcome: a viewer may chat, so a
    # viewer must be able to take back a misfired send, which is the misfire
    # case the phone composer exists for. Project turns likewise have no agent.
    if turn.agent_id:
        _agent_for_write_or_404(request, turn.agent.slug)
    elif turn.initiator_user_id != request.user.pk:
        # Not your own send: a chat turn needs write access to that chat (a
        # viewer participant may not withdraw someone else's message), and a
        # project turn the same tier that enqueued it.
        if turn.chat_session_id:
            from apps.canopy_sessions import access as session_access

            if not session_access.can_write(request.user, turn.chat_session):
                raise HttpError(403, "you can only cancel your own send in this chat")
        elif not perms.can(request.user, turn.workspace_id, perms.AGENT_WORK):
            raise HttpError(403, "cancelling a turn requires the editor role or above")
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

    The tenant is derived from `owner` — the runner's owner —
    rather than the Runner.workspace FK, because owner is server-assigned at
    pairing (request.user), so the FIELD is not attacker-controlled.

    That last point is necessary but NOT sufficient, and reading it alone is how
    this route was first shipped vulnerable. Deriving the tenant from an
    unspoofable field on a row the ATTACKER SELECTED buys nothing: runner_id is
    a caller-supplied query param, so choosing whose owner gets read is as
    good as spoofing it. The real invariant needs both halves — the tenant
    derives from owner AND _runner_or_404 pins the runner to request.user,
    so the row and the field are alike server-controlled.

    claim_next_turn DERIVES FROM THE SAME TWO FUNCTIONS this does —
    services.runner_tenant_slugs and services.agent_tenant_q — so the two rules
    AGREE BY CONSTRUCTION: every schedule this runner may fire produces a turn
    that same runner may claim. They are no longer two hand-written predicates
    that happen to match; there is one predicate with two callers.

    That matters because they briefly diverged, and the divergence was an
    outage, not a nicety. claim_next_turn shipped scoped to the Runner.workspace
    FK while this predicate derived from owner — so a runner homed to
    `alpha` whose owner also belongs to `beta` could SEE and FIRE beta's
    schedules here but could not CLAIM the resulting turns, leaving them QUEUED
    forever. Because one laptop runner serves a fleet that deliberately spans
    workspaces, that stopped 4 of 5 production agents from executing at all. The
    resolution was to converge the CLAIM onto owner (this predicate's rule),
    NOT to narrow this one onto the FK: the FK records where a runner lives, not
    who it may work for. tests/test_claim_schedule_parity.py fails if the two
    ever disagree again.

    NULL owner fails closed inside runner_tenant_slugs (empty slug set →
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
    try:
        turn, _ = services.fire_schedule(schedule, payload.slot)
    except services.OneOffSlotMismatch as exc:
        raise HttpError(409, str(exc)) from None
    return Status(201, turn)


# --------------------------------------------------------------------------------------
# Readiness drills — a hard-pinned, read-only doctor turn per (runner, agent), resolved
# by the drilled agent's own report callback or by the turn failing (see
# services.start_drill / finish_turn). Spec 2026-07-24-directed-runner-routing, Task 7.
# --------------------------------------------------------------------------------------


@router.post("/runners/{runner_id}/drill", response=list[RunnerDrillOut])
def start_runner_drill(request: HttpRequest, runner_id: uuid.UUID, payload: DrillIn):
    """Fan out a readiness drill. Runner side: the runner's owner or a
    `RunnerAdmin` (`_runner_admin_or_404`) — starting a drill only QUEUES pinned
    turns; the box still claims and runs them as itself, so administering it is
    enough. Agent side: someone other than the owner must also be an ADMIN of
    every agent drilled (`Agent.is_admin`) — a drill turn runs as `system`, in
    the agent's routing mode, so an editor-tier caller (always `manual`,
    docs/architecture/access.md row 4) must not get one started on their behalf.

    Default: every assigned agent the caller may drill; body.agents narrows by
    slug, and naming one the caller may not drill is a 403 saying which.
    Deliberately includes DISABLED assignment rows too — drill-before-enable is
    the intended workflow (prove a standby actually works before flipping it
    live), so a disabled row must stay drillable even though it can never claim
    routed traffic."""
    runner = _runner_admin_or_404(request, runner_id)
    # No enabled=True filter here on purpose — see the docstring above.
    # An agent following a workspace order that lists this runner counts too.
    assigned = Agent.objects.filter(
        Q(runner_assignments__runner=runner) | Q(id__in=services.agents_following_runner(runner))
    ).distinct()
    # `is not None` (not truthy) so an explicit [] narrows to "drill nothing" and
    # hits the 422 below, rather than being treated the same as "drill everyone".
    agents = list(assigned.filter(slug__in=payload.agents) if payload.agents is not None else assigned)
    # The owner is not re-asked here: their box can only CLAIM an agent's turn
    # when they are that agent's admin (`runner_may_hold_agent`), so the claim
    # gate already holds for them, and this route's owner behaviour is unchanged.
    if runner.owner_id != request.user.id:
        refused = [a.slug for a in agents if not a.is_admin(request.user)]
        if refused and payload.agents is not None:
            raise HttpError(
                403,
                f"not an admin of {', '.join(sorted(refused))} — a readiness drill on an agent "
                "needs its admin (or the runner's owner)",
            )
        agents = [a for a in agents if a.slug not in refused]
    if not agents:
        raise HttpError(422, "no assigned agents to drill — assign this runner to an agent first"
                             " (a runner admin drills only agents they administer)")
    return services.start_drill(runner, agents, started_by=request.user)


@router.get("/runners/{runner_id}/drills", response=list[RunnerDrillOut])
def list_runner_drills(request: HttpRequest, runner_id: uuid.UUID):
    # Readiness results are a log about the box: its owner and the admins it
    # granted read them (the same tier that STARTS drills), and so does a
    # workspace admin of the runner's tenant (`permissions.LOGS_READ`). It used
    # to be the owner alone, so nobody operating the fleet could see why a box
    # was failing its drills. A plain member/viewer gets the no-leak 404.
    runner = (Runner.objects.exclude(status=Runner.RETIRED)
              .filter(_runner_read_q(request)).filter(pk=runner_id).first())
    if runner is None or not (
        services.can_administer_runner(request.user, runner)
        or (runner.workspace_id and perms.can(request.user, runner.workspace_id, perms.LOGS_READ))
    ):
        raise HttpError(404, "runner not found")
    return list(runner.drills.select_related("agent"))


@router.post("/drills/{drill_id}/report", response=RunnerDrillOut)
def report_drill(request: HttpRequest, drill_id: int, payload: DrillReportIn, t: str = ""):
    """The drilled agent's callback: accepted with the run's signed report link
    (`t`), from the drilled agent's own login, or from the runner's owner."""
    # Two callers, both legitimate. An agent with its own canopy login
    # (`Agent.user`, per-agent PATs) reports AS ITSELF and correctly refuses to
    # borrow the operator's token — gating on the runner owner alone 404'd that
    # report and stranded the drill (cloud-ec2-1, 2026-09-22). The owner leg stays
    # for agents with no login, which run on the owner's token. Anyone else gets
    # the same 404 as before, so a drill's existence never leaks.
    drill = get_object_or_404(
        RunnerDrill.objects.select_related("runner", "agent"), pk=drill_id
    )
    # The signed link comes first and is sufficient: it names this one run, so
    # it lets its holder report that run and nothing else — whichever canopy
    # login the agent happens to authenticate with.
    if not services.drill_report_token_ok(drill, t):
        agent_user_id = drill.agent.user_id
        if agent_user_id is None or agent_user_id != request.user.id:
            _runner_or_404(request, drill.runner_id)  # the owner gate; 404 on non-owner
    return services.report_drill(drill, outcome=payload.outcome, summary=payload.summary)
