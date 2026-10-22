"""Django Ninja router for /api/harness — runner registry + turn lifecycle."""
from __future__ import annotations

import logging
import uuid

from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404
from ninja import Router, Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_read
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.pagination import Page, clamp_limit, paginate
from apps.canopy_sessions.models import Session
from apps.huddles.models import CheckinRun
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import drills, routing, schedule_turns
from . import runners as harness_runners
from .models import (
    AgentSchedule,
    Runner,
    RunnerDrill,
    Turn,
)
from .schedule_services import serialize_schedule
from .schemas import (
    DrillIn,
    DrillReportIn,
    RunnerDrillOut,
    ScheduleAnnouncedOut,
    ScheduleFireIn,
    ScheduleOut,
    TurnOut,
)

logger = logging.getLogger(__name__)

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
    # A caller's turn's `For <Agent>'s owner:` line, lifted into result_note
    # (owner_ask.py, canopy-web#1416).
    "owner_ask",
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


def _runner_owned_q(request: HttpRequest) -> Q:
    """Ownership: the caller owns it. A runner NOBODY owns is acted on by
    nobody — it used to be acted on by everyone (heartbeat, claim, retire, and
    its plaintext credential bundle), the NULL-means-allow shape this repo has
    removed everywhere else. Every live runner on labs has a owner (2026-10-02)."""
    from .runner_protocol import owned_runner_q

    return owned_runner_q(request.user)


#: Fleet runners only: a computer's key holder (`Runner.COMPUTER` — a person's
#: computer in plain Claude Code, no runner app) is not a box anyone lists, pins,
#: heartbeats, claims as or administers; its keys are reached through
#: `/api/runner-device-keys/` and its machine through `/api/private-runners/`.
_FLEET_Q = ~Q(kind=Runner.COMPUTER)


def runner_read_q(request: HttpRequest) -> Q:
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
        # Tenant-pinned: homed here, or SERVING here (`_runners_serving_q`), and
        # no null-workspace leg at all — a null-workspace runner is wrong-tenant
        # here, not ungated (the property
        # test_pinned_null_workspace_runner_is_neither_listed_nor_actionable
        # pins). WorkspaceResolveMiddleware has already gated membership of `ws`.
        return (Q(workspace_id=ws) | _runners_serving_q({ws})) & _FLEET_Q
    slugs = wsvc.user_workspace_slugs(request.user)
    return (
        Q(workspace_id__in=slugs)
        | _runners_serving_q(slugs)
        | (Q(workspace_id__isnull=True) & _runner_owned_q(request))
    ) & _FLEET_Q


def _runners_serving_q(slugs) -> Q:
    """Runners the claim path would hand a turn in any of `slugs` — the tenant
    gate itself (`services.runner_tenant_slugs`: owner's `agent.work` ∩
    enrollment coverage), not `Runner.workspace`, which only records where a box
    LIVES. The fleet's cloud boxes live in `dimagi` and serve `connect`; an
    exact match on the home workspace listed them nowhere in `connect` and
    422'd a turn pinned to one there, while claim gave the same box the same
    turn unpinned (#1447).

    Read-only widening: everything that speaks FOR a runner still gates on
    `_runner_visibility_q` (ownership). A runner with no home workspace stays
    out, as the read predicate has always kept it; a NULL owner fails closed
    inside `runner_tenant_slugs`."""
    slugs = set(slugs)
    if not slugs:
        return Q(pk__in=[])
    candidates = (Runner.objects.exclude(status=Runner.RETIRED).filter(_FLEET_Q)
                  .filter(owner__isnull=False, workspace__isnull=False)
                  .select_related("owner").prefetch_related("enrollments"))
    return Q(pk__in=[r.pk for r in candidates if routing.runner_tenant_slugs(r) & slugs])


def _runner_visibility_q(request: HttpRequest) -> Q:
    """'Runners this caller can ACT ON' — the tenant AND ownership. Unchanged.

    Heartbeating, claiming as, mutating, retiring and crediting a runner gate on
    this. Ownership is the boundary because these operations speak FOR the runner:
    `owner` is what `claim_next_turn` derives a tenant from, so acting as
    someone else's runner is acting with their memberships.

    `runner_read_q` is deliberately WIDER, which gives up the invariant these two
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

        here = Workspace.objects.filter(slug=ws).first()
        wq = Q(workspace_id__in=[ws, *(here.ancestor_slugs() if here else [])])
    else:
        wq = Q(workspace_id__in=wsvc.user_workspace_slugs(request.user)) | Q(workspace_id__isnull=True)
    return wq & _runner_owned_q(request) & _FLEET_Q


def runner_admin_or_404(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    """Resolve a runner this caller may ADMINISTER — a wider tier than acting AS
    it, and deliberately not the same predicate.

    Reached from the operator-facing routes only: setting credentials, the
    browser sign-in, and STARTING a readiness drill (owner decision 2026-10-04:
    "let admins of a runner also run readiness checks"). Everything that speaks
    FOR the runner — heartbeat, claim, executing a drill's turns, the runner's
    own credential fetch and its half of a mint — keeps `runner_or_404`,
    because those act with the owner's memberships.

    Starts from what the caller can SEE (the tenant), then requires the explicit
    grant on top. Both legs matter: the tenant leg stops a grant in one workspace
    reaching a runner in another, and the grant leg stops the 22 auto-joined
    members of a `dimagi.com` workspace inheriting its credentials.
    """
    runner = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(runner_read_q(request))
        .filter(pk=runner_id)
        .first()
    )
    # 404 rather than 403, matching runner_or_404: the harness must not leak
    # which runners exist to someone who may not act on them.
    if runner is None or not harness_runners.can_administer_runner(request.user, runner):
        raise HttpError(404, "runner not found")
    return runner


def runner_or_404(
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


def turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """`_tenant_turn_or_404`, then `site ∩ user`: a connected site acting for its
    visitor sees only its own agents' turns (apps/embedding/delegation.py). Same
    uniform 404, so a site cannot learn another agent's turn exists."""
    from apps.embedding import delegation

    turn = _tenant_turn_or_404(request, turn_id)
    offered = delegation.offered_for(request)
    if offered is not None and delegation.turn_agent_id(turn) not in offered:
        raise HttpError(404, "turn not found")
    return turn


def agents_own_thread_q():
    """Session turns on an AGENT's own thread (an email or Slack thread: a runner-
    origin session nobody created), which every member of its workspace may see
    ran — the same interaction tier as an agent turn. Before #734 these were agent
    turns and the whole tenant listed them; converting them to session turns hid
    them from everyone but the agent's admins, and a member's routing audit read
    the silence as "0 turns" (#1087). Their CONTENT stays gated by the session
    (`turn_access.redact`), and a person's own chat is not one of these."""
    return Q(chat_session__origin=Session.ORIGIN_RUNNER, chat_session__created_by__isnull=True,
             chat_session__agent__isnull=False)


def visible_sessions(request: HttpRequest):
    """Chat sessions the caller may read — the chat ACL, as a subquery, for
    every harness listing that would otherwise show a session turn's prompt or
    a session's messages to the whole tenant."""
    from apps.canopy_sessions import access as session_access

    return Session.objects.filter(session_access.visible_session_q(request.user)).values("pk")


def turn_content_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """A turn whose CONTENT (ledger, transcript, caller context) the caller may
    read (`turn_access.can_read_turn_content`) — a log, the admin's unless you
    started or run it. Same uniform 404 as a turn you cannot see at all."""
    from . import turn_access

    turn = turn_or_404(request, turn_id)
    if not turn_access.can_read_turn_content(request.user, turn):
        raise HttpError(404, "turn not found")
    return turn


def reporting_turn_or_404(request: HttpRequest, turn_id: uuid.UUID) -> Turn:
    """A turn the caller may REPORT on — start, finish, append ledger events or
    transcript lines. That is the runner protocol, so it belongs to the box that
    claimed the turn: the caller must be its owner.

    `turn_or_404` alone (membership) let any viewer fail someone else's live
    turn, inject `question`/`approval` events, or append fake transcript lines.
    An UNCLAIMED turn has no box yet; reporting on one is a write to the work
    itself, so it takes the same tier as cancelling it (editor on an agent turn,
    write access to the chat on a session turn). Same uniform 404 otherwise.
    """
    # The box that CLAIMED the turn reports on it, even when its owner cannot READ
    # it (canopy-web#1210). The claim is tenant- and routing-gated already, and it
    # handed the box the prompt; refusing the report then only stranded the turn
    # CLAIMED until its lease ran out — observed when a runner paired by one person
    # claimed a private chat another member created. Not for a connected site
    # acting for a visitor: a site never reports as a runner.
    from apps.embedding import delegation

    rtok = getattr(request, "runner_token", None)
    if rtok is not None and rtok.runner.kind == Runner.COMPUTER:
        # A computer (no runner app) claims nothing: its token reports only on the
        # local turns opened on it — never on a turn its owner's other boxes hold.
        turn = Turn.objects.filter(pk=turn_id, origin=Turn.ORIGIN_LOCAL).select_related(
            "private_runner").first()
        from . import local

        if turn is None or not local.may_report(turn, request):
            raise HttpError(404, "turn not found")
        return turn
    if delegation.offered_for(request) is None:
        mine = (Turn.objects.select_related("claimed_by")
                .filter(pk=turn_id, claimed_by__owner_id=request.user.pk).first())
        if mine is not None:
            return mine
    turn = turn_or_404(request, turn_id)
    if turn.origin == Turn.ORIGIN_LOCAL:
        # A local turn has no box: the person whose session it is reports on it
        # (apps/harness/local.py), and nobody else — not even an editor, nor their
        # shared runner's token.
        from . import local

        if not local.may_report(turn, request):
            raise HttpError(404, "turn not found")
        return turn
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


def site_turn_q(request: HttpRequest):
    """The turns an acting site may see, as a Q — or None when no site acts."""
    from apps.embedding import delegation

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
    `agent_for_read` (security review 2026-07-26, F4): that helper's own 404
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
            agent_for_read(request, turn.agent.slug)  # raises on wrong tenant
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

    # Project turn: gate on its own workspace, mirroring agent_for_read's checks.
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


#: Capabilities a box REPORTS on heartbeat and never declares at pairing.
REPORTED_CAPABILITIES = ("profiles", "envelope", "midturn", "data_mode", "data_mode_hash")


def declared_capabilities(caps: dict) -> dict:
    """What a pairing box may declare. `profiles` is REPORTED on heartbeat, never
    declared: a box that says at pairing it can confine a caller's session would
    be believed until its first beat. Dropped rather than refused, so an old
    pairing script that copies a whole capabilities dict still pairs."""
    return {k: v for k, v in (caps or {}).items() if k not in REPORTED_CAPABILITIES}


# The routes live in their own modules but on THIS router, registered in this
# order (route order is path-match order and the schema's order).
from . import api_runners, api_session_sync, api_turns  # noqa: E402,F401


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
    derives from owner AND runner_or_404 pins the runner to request.user,
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
    return qs.filter(routing.agent_tenant_q(routing.runner_tenant_slugs(runner)))


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
    runner = runner_or_404(request, runner_id)
    return paginate(_runner_schedule_qs(runner).order_by("name", "pk"), offset=0,
                    limit=clamp_limit(limit), map=lambda s: ScheduleOut(**serialize_schedule(s)))


@router.post("/schedules/{schedule_id}/fire", response={201: TurnOut, 202: ScheduleAnnouncedOut},
             summary="Report a due slot; the server materializes the turn")
def fire_schedule_route(
    request: HttpRequest, schedule_id: int, runner_id: uuid.UUID, payload: ScheduleFireIn
) -> Status:
    runner = runner_or_404(request, runner_id)
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
        turn, _ = schedule_turns.fire_schedule(schedule, payload.slot)
    except schedule_turns.OneOffSlotMismatch as exc:
        raise HttpError(409, str(exc)) from None
    if turn is None:
        # A project check-in's schedule makes a run instead: with a channel it is
        # announced there and the turn comes once the lead confirms; without one
        # the check-in queued the turn itself (the run reads "running").

        run = CheckinRun.objects.filter(checkin__schedule=schedule, slot=payload.slot).first()
        return Status(202, ScheduleAnnouncedOut(announced=True, run_id=getattr(run, "pk", None),
                                                status=getattr(run, "status", "")))
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
    runner = runner_admin_or_404(request, runner_id)
    # No enabled=True filter here on purpose — see the docstring above.
    # An agent following a workspace order that lists this runner counts too.
    assigned = Agent.objects.filter(
        Q(runner_assignments__runner=runner) | Q(id__in=routing.agents_following_runner(runner))
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
    return drills.start_drill(runner, agents, started_by=request.user)


@router.get("/runners/{runner_id}/drills", response=list[RunnerDrillOut])
def list_runner_drills(request: HttpRequest, runner_id: uuid.UUID):
    # Readiness results are a log about the box: its owner and the admins it
    # granted read them (the same tier that STARTS drills), and so does a
    # workspace admin of the runner's tenant (`permissions.LOGS_READ`). It used
    # to be the owner alone, so nobody operating the fleet could see why a box
    # was failing its drills. A plain member/viewer gets the no-leak 404.
    runner = (Runner.objects.exclude(status=Runner.RETIRED)
              .filter(runner_read_q(request)).filter(pk=runner_id).first())
    if runner is None or not (
        harness_runners.can_administer_runner(request.user, runner)
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
    if not drills.drill_report_token_ok(drill, t):
        agent_user_id = drill.agent.user_id
        if agent_user_id is None or agent_user_id != request.user.id:
            runner_or_404(request, drill.runner_id)  # the owner gate; 404 on non-owner
    return drills.report_drill(drill, outcome=payload.outcome, summary=payload.summary)
