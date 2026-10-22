"""/api/harness runner routes: pairing, the fleet list, a runner's logins and
credential, its Claude sign-in (mint, sign-in requests), admins, enrolled
workspaces, flags, retire/pause, the fleet hold, heartbeat, usage, and the claim.

Registered on the harness router (`api.router`), in the order api.py imports
this module, so the schema and the MCP tool names are unchanged."""
from __future__ import annotations

import logging
import uuid

from canopy_sdk import contract
from django.conf import settings
from django.contrib.auth.models import User
from django.db import models, transaction
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Status
from ninja.errors import HttpError

from apps.api.errors import ProblemError
from apps.canopy_sessions.models import Attachment, Session
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import claim, routing, services
from . import runners as harness_runners

# The one harness router and the helpers every harness route shares; api.py
# imports this module after defining them.
from .api import (  # noqa: E402
    declared_capabilities,
    router,
    runner_admin_or_404,
    runner_or_404,
    runner_read_q,
)
from .models import (
    FleetHold,
    PrivateRunner,
    Runner,
    RunnerAssignment,
    Turn,
    WorkspaceRunnerOrder,
)
from .schemas import (
    ClaimedTurnOut,
    FleetHoldIn,
    FleetHoldOut,
    HeartbeatIn,
    MissingSessionsOut,
    OpenSignInRequestOut,
    PauseIn,
    RetireOut,
    RunnerAdminIn,
    RunnerAdminOut,
    RunnerCapabilitiesIn,
    RunnerCredentialIn,
    RunnerCredentialOut,
    RunnerCredentialStatusOut,
    RunnerEngineIn,
    RunnerEnrollmentIn,
    RunnerEnrollmentOut,
    RunnerFlagsIn,
    RunnerGitHubReadinessOut,
    RunnerIn,
    RunnerLoginOrderIn,
    RunnerLoginReportIn,
    RunnerMintClaimOut,
    RunnerMintCodeIn,
    RunnerMintOut,
    RunnerMintResultIn,
    RunnerMintStartIn,
    RunnerMintUrlIn,
    RunnerOut,
    RunnerPrivateIn,
    SendUpIn,
    SendUpOut,
    SignInRequestIn,
    SignInRequestOut,
    SignInResultIn,
    SignInResultOut,
    TurnGitHubTokenOut,
    UsageReportIn,
    UsageReportOut,
    UsageScopeOut,
)

logger = logging.getLogger(__name__)

#: Published operationIds keep the label of the module these routes moved out of
#: (apps/api/api.py `_app_label`): an operationId is an MCP tool name.
OPERATION_LABEL = "harness"


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
        capabilities=declared_capabilities(payload.capabilities),
        host=payload.host,
        owner=request.user,
        workspace_id=ws_slug,
    )
    return Status(201, _with_roots(request, runner))


@router.post("/runners/{runner_id}/credential", response=RunnerCredentialStatusOut,
             summary="Set a cloud runner's credential bundle (owner only)")
def set_runner_credential(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerCredentialIn):
    """Store the per-runner secrets a cloud runner fetches at startup — its Claude
    login (plus the secondary subscription and API key it fails over to).
    Owner-gated exactly
    like heartbeat/claim (owner == caller). Non-clobbering per field. Encrypted
    at rest; the response is masked (booleans, never values)."""
    runner = runner_admin_or_404(request, runner_id)
    if harness_runners.would_remove_last_backup_token(
            runner, claude_token=payload.claude_token,
            claude_token_secondary=payload.claude_token_secondary):
        raise HttpError(422, harness_runners.LAST_BACKUP_TOKEN_REFUSAL)
    harness_runners.set_runner_credential(
        runner,
        claude_token=payload.claude_token,
        claude_token_secondary=payload.claude_token_secondary,
        claude_api_key=payload.claude_api_key,
        claude_token_label=payload.claude_token_label,
        claude_token_secondary_label=payload.claude_token_secondary_label,
        updated_by=request.user,
    )
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/credential/swap", response=RunnerCredentialStatusOut,
             summary="Swap the primary and fallback Claude logins")
def swap_runner_logins(request: HttpRequest, runner_id: uuid.UUID):
    """The fallback becomes the primary and the primary the fallback — each
    login keeps its name. The runner reads the new order the next time it
    re-reads its bundle."""
    runner = runner_admin_or_404(request, runner_id)
    harness_runners.swap_runner_logins(runner, updated_by=request.user)
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/logins", response=RunnerCredentialStatusOut,
             summary="Add a Claude login to a runner and start signing it in")
def add_runner_login(request: HttpRequest, runner_id: uuid.UUID):
    """A new full Claude login, last in the runner's cascade. The runner picks up
    the sign-in on its next poll (see `get_runner_mint`); the login reads
    "not signed in" until it finishes."""
    runner = runner_admin_or_404(request, runner_id)
    harness_runners.add_runner_login(runner, by=request.user)
    return harness_runners.runner_credential_status(runner)


# The fixed paths are declared before `/logins/{key}`, which would match them.
@router.put("/runners/{runner_id}/logins/order", response=RunnerCredentialStatusOut,
            summary="Set the order a runner tries its Claude logins in")
def order_runner_logins(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerLoginOrderIn):
    """`keys` is every login on the runner, first to last. When the first hits
    its usage cap the runner moves to the next."""
    runner = runner_admin_or_404(request, runner_id)
    try:
        harness_runners.order_runner_logins(runner, payload.keys)
    except harness_runners.LoginOrderRefused as exc:
        raise HttpError(422, str(exc)) from exc
    return harness_runners.runner_credential_status(runner)


@router.post("/runners/{runner_id}/logins/report", response=RunnerCredentialStatusOut,
             summary="The runner reports which of its logins are signed in")
def report_runner_logins(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerLoginReportIn):
    """Which logins the box holds and as which account."""
    runner = runner_or_404(request, runner_id)
    harness_runners.report_runner_logins(
        runner, [r.dict() for r in payload.logins], adopt_default=payload.adopt_default)
    return harness_runners.runner_credential_status(runner)


@router.delete("/runners/{runner_id}/logins/{key}", response=RunnerCredentialStatusOut,
               summary="Remove a Claude login from a runner")
def remove_runner_login(request: HttpRequest, runner_id: uuid.UUID, key: str):
    """The runner signs the login out on the box the next time it re-reads its
    credentials, within a few minutes."""
    runner = runner_admin_or_404(request, runner_id)
    if not harness_runners.remove_runner_login(runner, key):
        raise HttpError(404, "no such login on this runner")
    return harness_runners.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential/status", response=RunnerCredentialStatusOut,
            summary="Which credential slots are set (masked — booleans, never values)")
def get_runner_credential_status(request: HttpRequest, runner_id: uuid.UUID):
    """The operator's read: which slots are filled, without putting a secret on a
    screen. Separate from the GET below, which returns REAL VALUES for the runner
    to consume.

    Exists because the only way to read this used to be a no-op POST — and that
    WROTE, bumping `updated_at`/`updated_by` and destroying the one signal that
    says when a credential was last actually rotated."""
    runner = runner_admin_or_404(request, runner_id)
    return harness_runners.runner_credential_status(runner)


@router.get("/runners/{runner_id}/credential", response=RunnerCredentialOut,
            summary="Fetch this runner's credential bundle (the runner, via its PAT)")
def get_runner_credential(request: HttpRequest, runner_id: uuid.UUID) -> RunnerCredentialOut:
    """A cloud runner fetches its own secrets to stage into its environment. Returns
    the actual token values over HTTPS, gated to the runner's owner (owner ==
    caller) — the same trust boundary that lets that caller claim turns as the
    runner. Laptop/emdash runners never call this (they use ambient auth)."""
    runner = runner_or_404(request, runner_id)
    return RunnerCredentialOut(**harness_runners.get_runner_credential(runner))


@router.post("/runners/{runner_id}/turns/{turn_id}/github-token",
             response=TurnGitHubTokenOut,
             summary="One claimed turn's GitHub credential (its agent owner's)")
def turn_github_token(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID):
    """The GitHub token and git identity for ONE turn this runner is executing:
    the turn's agent owner's token for that agent. canopy decides whose — the
    runner only names the turn. Refused (409, with the reason) when the owner
    has lent none or it has expired, and always for a caller's turn (one confined
    to a capability); there is no shared fallback."""
    from apps.agents import delegations

    runner = runner_or_404(request, runner_id)
    # Only a turn this runner holds right now. A finished turn, or one another
    # box claimed, gets the same 404 as one that does not exist.
    turn = (
        Turn.objects.select_related("agent__owner", "chat_session__agent__owner",
                                    "initiator_user", "initiator_contact")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
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


@router.get("/runners/{runner_id}/turns/{turn_id}/attachments/{attachment_id}/content",
            summary="An attachment from the chat a turn this runner holds belongs to")
def turn_attachment_content(request: HttpRequest, runner_id: uuid.UUID, turn_id: uuid.UUID,
                            attachment_id: uuid.UUID):
    """The bytes of a file a person sent in the chat this runner is serving, so
    the agent can open it. Allowed while this runner holds the turn, for any
    attachment already sent in that turn's chat — earlier messages included, so
    a box with no local copy (a fresh cloud machine) can still fetch them.
    Everything else is the same 404 as an id that does not exist."""
    # Authorized by the TURN, not by whose token the box carries: the runner
    # speaks as its owner, and its owner need not be able to read the chat (a
    # teammate's private chat with an agent is visible to its creator and
    # participants only, canopy_sessions/access.py). Claiming the turn is what
    # entitles the box to it — the same rule as turn_github_token above.
    from apps.agents import delegations
    from apps.agents.services import runner_may_hold_agent
    from apps.canopy_sessions import attachment_storage

    runner = runner_or_404(request, runner_id)
    turn = (
        Turn.objects.select_related("agent", "chat_session__agent")
        .filter(pk=turn_id, claimed_by=runner, status__in=routing.EXECUTING)
        .first()
    )
    if turn is None or turn.chat_session_id is None:
        raise HttpError(404, "attachment not found")
    # A claim already requires this; re-asked because an owner can be demoted
    # mid-turn.
    agent = delegations.turn_agent(turn)
    if agent is not None and not runner_may_hold_agent(runner, agent):
        raise HttpError(404, "attachment not found")
    # Sent only: an unsent attachment is still someone's draft (sent_at, not
    # message, marks a send — a runner-origin session writes no Message row).
    attachment = Attachment.objects.filter(
        pk=attachment_id, session_id=turn.chat_session_id, sent_at__isnull=False,
    ).first()
    if attachment is None:
        raise HttpError(404, "attachment not found")
    return attachment_storage.http_response(attachment)


@router.get("/runners/{runner_id}/github-readiness",
            response=list[RunnerGitHubReadinessOut],
            summary="Can each agent this runner serves open a pull request?")
def runner_github_readiness(request: HttpRequest, runner_id: uuid.UUID):
    """Checked live against GitHub, per agent routed to this runner. A box calls
    this when it boots so a missing, expired or under-scoped token is a health
    check going red, not a 403 in the middle of a turn."""
    from apps.agents import delegations

    runner = runner_or_404(request, runner_id)
    out = []
    for agent in routing.agents_routed_to(runner):
        delegations.check_github(agent)
        status, detail = delegations.readiness(agent)
        st = delegations.status(agent)
        out.append({"agent_slug": agent.slug, "status": status, "detail": detail,
                    "login": st.get("login", ""), "expires_at": st.get("expires_at")})
    return out


@router.get("/runners/{runner_id}/agents", response=list[str],
            summary="The agents this runner runs: the ones it should prepare")
def runner_agents(request: HttpRequest, runner_id: uuid.UUID) -> list[str]:
    """Slugs of the agents this box RUNS (`routing.agents_routed_to`): the
    claim's own static gates, so a box prepares exactly what it can be handed.
    The box's refresh prepares only these: listing every agent its token could
    see made it ask for vaults it may not open, and each refusal emailed that
    agent's owner (canopy-web#1563)."""
    from . import device_keys

    runner = runner_or_404(request, runner_id)
    return [a.slug for a in device_keys.agents_served(runner)]


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

    Two kinds, for two jobs:
    - `box` (the default): a full `claude auth login` the runner keeps for
      itself — the only kind that carries claude.ai connectors. It renews
      itself, so it can also stop renewing (cloud-ec2-2, 2026-10-15).
    - `primary`/`secondary`: a `claude setup-token` into a backup-token slot.
      Inference only, but good for a year with nothing to renew — what keeps the
      box running when the full login fails. Retired on 2026-10-14 because the
      page offered it beside the full login under the same words; it is back
      under its own ("Create a long-lived token"), since without it a box whose only
      backup was removed had no way to get one but a terminal.
    """
    runner = runner_admin_or_404(request, runner_id)
    slot = payload.slot if payload else "box"
    login_key = (payload.login_key if payload else "") if slot == "box" else ""
    if login_key and not runner.logins.filter(key=login_key).exists():
        raise HttpError(404, "no such login on this runner")
    return harness_runners.start_runner_mint(runner, requested_by=request.user, slot=slot,
                                      login_key=login_key)


@router.get("/runners/{runner_id}/mint", response=RunnerMintOut | None,
            summary="The current sign-in attempt, if any")
def get_runner_mint(request: HttpRequest, runner_id: uuid.UUID):
    """What the waiting human's screen renders: whether the URL is up yet, and
    how the attempt ended. Null when no sign-in has ever been started."""
    runner = runner_admin_or_404(request, runner_id)
    return harness_runners.current_runner_mint(runner)


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
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.claim_runner_mint(runner)
    if mint is None:
        return {"mint": None, "code": ""}
    return {"mint": mint, "code": harness_runners.take_mint_code(mint)}


@router.post("/runners/{runner_id}/mint/url", response=RunnerMintOut,
             summary="The runner reports the URL a human must open")
def post_runner_mint_url(request: HttpRequest, runner_id: uuid.UUID,
                         payload: RunnerMintUrlIn):
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None or mint.status in mint.FINISHED:
        raise HttpError(409, "no sign-in is in progress for this runner")
    return harness_runners.record_mint_url(mint, payload.url)


@router.post("/runners/{runner_id}/mint/code", response=RunnerMintOut,
             summary="A human submits the authorization code")
def post_runner_mint_code(request: HttpRequest, runner_id: uuid.UUID,
                          payload: RunnerMintCodeIn):
    """The one secret a human handles in this flow, and it is single-use."""
    runner = runner_admin_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None or mint.status != mint.AWAITING_CODE:
        raise HttpError(409, "this runner is not waiting for a code")
    if not payload.code.strip():
        raise HttpError(422, "the authorization code is empty")
    return harness_runners.submit_mint_code(mint, payload.code)


@router.post("/runners/{runner_id}/mint/result", response=RunnerMintOut,
             summary="The runner reports the outcome (and delivers the token)")
def post_runner_mint_result(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerMintResultIn):
    """The minted token arrives HERE, never in the browser — it goes straight
    into the encrypted credential bundle, so the only secret that ever reaches a
    human's screen is the single-use authorization code."""
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None:
        raise HttpError(409, "no sign-in is in progress for this runner")
    return harness_runners.finish_runner_mint(mint, token=payload.token, detail=payload.detail,
                                       logged_in=payload.logged_in, account=payload.account)


@router.post("/runners/{runner_id}/sign-in-request", response=SignInRequestOut,
             summary="Ask the runner's owner to approve a cloud sign-in from their phone")
def post_sign_in_request(request: HttpRequest, runner_id: uuid.UUID, payload: SignInRequestIn):
    """The box started a device-code sign-in (`aws sso login --use-device-code`)
    and needs a person to approve it. This pushes the approval link to the
    runner's owner, and tapping it opens the AWS page with the code filled in.
    The CLI on the box finishes on its own once the person approves.

    `reason` is required (422 without it). The notification names who is
    asking, the runner, and why, so the owner can decide from the lock screen.

    `sent` is how many devices the notification reached. 0 means nobody saw
    it, so the caller falls back to another channel. The request is also kept
    until its code expires and shown on the owner's supervisor feed; `id` is
    what the box sends back to `sign-in-requests/{id}/result` when it ends.
    See `apps/harness/sign_in_requests.py` for why it goes only to the owner
    and only to AWS hosts.
    """
    from . import sign_in_requests

    runner = runner_or_404(request, runner_id)
    try:
        row, sent = sign_in_requests.push_sign_in_request(
            runner, provider=payload.provider, url=payload.url,
            label=payload.label, requested_by=payload.requested_by,
            reason=payload.reason)
    except sign_in_requests.SignInRequestError as exc:
        raise HttpError(422, str(exc)) from exc
    return {"sent": sent, "id": str(row.pk) if row else ""}


@router.post("/runners/{runner_id}/sign-in-requests/{request_id}/result", response=SignInResultOut,
             summary="The box reports how a sign-in request ended")
def post_sign_in_request_result(request: HttpRequest, runner_id: uuid.UUID, request_id: uuid.UUID,
                                payload: SignInResultIn):
    """`approved`, `failed` or `expired`. Takes the request off the owner's
    supervisor feed and clears its URL. Reporting twice keeps the first outcome."""
    from . import sign_in_requests

    runner = runner_or_404(request, runner_id)
    try:
        row = sign_in_requests.resolve(runner, request_id, payload.outcome)
    except sign_in_requests.SignInRequestError as exc:
        raise HttpError(422, str(exc)) from exc
    if row is None:
        raise HttpError(404, "sign-in request not found")
    return {"id": str(row.pk), "status": row.status}


@router.get("/sign-in-requests/mine", response=list[OpenSignInRequestOut],
            summary="Sign-ins your boxes are waiting on you to approve")
def list_my_sign_in_requests(request: HttpRequest):
    """Only the caller's own: the requests their runners put in front of them
    that are still open, newest first. What the supervisor feed shows beside
    the push, for when the phone never displayed it."""
    from . import sign_in_requests

    return [{"id": str(r.pk), "runner_id": str(r.runner_id), "runner_name": r.runner.name,
             "provider": r.provider, "label": r.label, "requested_by": r.requested_by,
             "reason": r.reason, "url": r.url, "sent": r.sent,
             "created_at": r.created_at, "expires_at": r.expires_at}
            for r in sign_in_requests.open_for(request.user)]

def _admin_row(a) -> dict:
    return {"user_id": a.user_id, "email": a.user.email,
            "granted_by_email": a.granted_by.email if a.granted_by else "",
            "created_at": a.created_at}


@router.get("/runners/{runner_id}/admins", response=list[RunnerAdminOut],
            summary="Who may administer this runner")
def list_runner_admins(request: HttpRequest, runner_id: uuid.UUID):
    """Visible to anyone who can already administer the box — the answer to
    "who else can fix this", which is the question a stuck box raises."""
    runner = runner_admin_or_404(request, runner_id)
    return [_admin_row(a) for a in harness_runners.list_runner_admins(runner)]


@router.post("/runners/{runner_id}/admins", response=RunnerAdminOut,
             summary="Grant someone administration of this runner (owner only)")
def grant_runner_admin(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerAdminIn):
    """Granting stays with the OWNER, not with grantees.

    Deliberate: an administrator can change what the box runs on, but letting
    them mint more administrators makes the grant self-propagating, and then the
    explicit list stops being a list of people the owner actually trusted.
    """
    runner = runner_or_404(request, runner_id)
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
    return _admin_row(harness_runners.grant_runner_admin(runner, user, granted_by=request.user))


@router.delete("/runners/{runner_id}/admins/{user_id}", response={204: None},
               summary="Revoke administration (owner only)")
def revoke_runner_admin(request: HttpRequest, runner_id: uuid.UUID, user_id: int):
    runner = runner_or_404(request, runner_id)
    user = User.objects.filter(pk=user_id).first()
    if user is None or not harness_runners.revoke_runner_admin(runner, user):
        raise HttpError(404, "no such grant on this runner")
    return Status(204, None)


def _enrollment_row(request: HttpRequest, runner: Runner, e, serving: set[str]) -> dict:
    below = wsvc.descendant_slugs({e.workspace_id}) & serving
    return {"workspace": e.workspace_id, "display_name": e.workspace.display_name,
            "serving": e.workspace_id in serving,
            "covers": sorted(s for s in below if _sees_enrollment(request, runner, s)),
            "enrolled_by_email": e.enrolled_by.email if e.enrolled_by else "",
            "created_at": e.created_at}


def _sees_enrollment(request: HttpRequest, runner: Runner, slug: str) -> bool:
    """The owner sees every workspace their box serves; an administrator sees
    only those they are a member of themselves — administering a box is not a
    reason to learn which other tenants its owner belongs to."""
    return runner.owner_id == request.user.id or wsvc.is_member(request.user, slug)


@router.get("/runners/{runner_id}/workspaces", response=list[RunnerEnrollmentOut],
            summary="The workspaces this runner serves (its enrollments)")
def list_runner_workspaces(request: HttpRequest, runner_id: uuid.UUID):
    """A runner serves a workspace only when it is ENROLLED there and its owner
    may run work there (editor and above) — `serving` says whether both hold.
    An enrollment covers the workspaces below it too, including ones created
    later (`covers` lists those it serves). Pairing enrolls a box only in the workspace it paired into; joining another
    workspace does not enroll your existing boxes."""
    runner = runner_admin_or_404(request, runner_id)
    serving = routing.runner_tenant_slugs(runner)
    return [_enrollment_row(request, runner, e, serving) for e in harness_runners.list_runner_enrollments(runner)
            if _sees_enrollment(request, runner, e.workspace_id)]


@router.post("/runners/{runner_id}/workspaces", response=RunnerEnrollmentOut,
             summary="Enroll this runner in a workspace so it serves its work")
def enroll_runner_workspace(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerEnrollmentIn):
    """Opt the box in to a workspace's turns — its agents' (as routing assigns
    them), its repo turns and its unbound chats — and those of every workspace
    below it, now or later, wherever its owner is an editor. Idempotent.

    The owner or a runner administrator may ask; the box's OWNER must be an
    editor or above there (422 otherwise), because a box works with its owner's
    memberships and enrollment never widens them. A caller who is not a member
    of the workspace gets the same 404 as a workspace that does not exist.
    """
    runner = runner_admin_or_404(request, runner_id)
    slug = (payload.workspace or "").strip()
    # The CALLER must be a member to name it — owner or administrator alike —
    # so a missing workspace and someone else's read the same.
    if not slug or not wsvc.is_member(request.user, slug):
        raise HttpError(404, f"workspace '{slug}' not found")
    try:
        row = harness_runners.enroll_runner(runner, slug, by=request.user)
    except harness_runners.EnrollmentRefused as exc:
        raise HttpError(422, str(exc))
    return _enrollment_row(request, runner, row, routing.runner_tenant_slugs(runner))


@router.delete("/runners/{runner_id}/workspaces/{workspace}", response={204: None},
               summary="Stop this runner serving a workspace")
def unenroll_runner_workspace(request: HttpRequest, runner_id: uuid.UUID, workspace: str):
    """The box claims nothing more there from its next poll — unless a
    workspace above it is still enrolled, which covers it; a turn it already
    holds runs to completion. Owner or a runner administrator."""
    runner = runner_admin_or_404(request, runner_id)
    if not _sees_enrollment(request, runner, workspace) or not harness_runners.unenroll_runner(
            runner, workspace):
        raise HttpError(404, "this runner is not enrolled in that workspace")
    return Status(204, None)


@router.put("/runners/{runner_id}/flags", response=RunnerOut,
            summary="Declare what this runner's owner vouches for")
def set_runner_flags(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerFlagsIn):
    """Replace the runner's declared flags. `zdr`: this box uses only
    zero-data-retention keys for Claude. canopy cannot check a declaration; it
    records who made it. A host may require a flag of every conversation its
    visitors hold, and those conversations then run only on runners declaring it.
    """
    runner = runner_admin_or_404(request, runner_id)
    # A person vouching for a box, never a token: not a PAT, not an MCP call, and
    # not the box's own RunnerAccessToken (which runner_auth lets reach every route
    # under its own runner). 403 `step_up_required` -> the SPA asks for the passkey
    # and retries; a token gets a plain 403 it cannot answer.
    from apps.passkeys import stepup

    stepup.require_recent_step_up(request)
    try:
        wanted = set(contract.parse_runner_requirements(payload.flags))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    added, removed = harness_runners.set_runner_flags(runner, wanted, by=request.user)
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
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/private", response=RunnerOut,
            summary="Make this runner its owner's private runner")
def set_runner_private(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerPrivateIn):
    """A private runner takes no fleet work: nothing routed, no workspace order, no
    source rule — only what its owner pins or binds to it, and the local sessions
    opened on it. Owner only. (To run as a lower role, start the TURN with
    `acts_as` — it is a property of one turn, not of a machine.)"""
    # canopy-web#1705. Owner only, not runner admins: it decides whose work the box
    # takes, which is the owner's to say.
    from apps.events import audit

    runner = runner_or_404(request, runner_id)
    if runner.owner_id != request.user.pk:
        raise HttpError(404, "only the runner's owner can make it private")
    before = {"private": runner.private}
    after = {"private": payload.private}
    if before != after:
        runner.private = payload.private
        runner.save(update_fields=["private"])
    if runner.private:
        # The box IS its owner's private runner: link the registration here, so the
        # runner writes it on its next heartbeat and no one runs `canopy private-runner
        # register` on the machine. Its OWN registration — never another machine's
        # that shares its name (`local.private_runner_for_runner`).
        from . import local

        local.private_runner_for_runner(runner)
    else:
        # Shared again: the registration stops naming a box whose token now speaks
        # for the fleet. Making it private again links (or makes) one afresh.
        PrivateRunner.objects.filter(runner=runner).update(runner=None)
    audit.note_change(scope=f"runner-private:{runner.name}", before=before, after=after,
                      workspace=runner.workspace)
    out = Runner.objects.prefetch_related("declared_flags").get(pk=runner.pk)
    out.can_manage = True
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


@router.put("/runners/{runner_id}/engine", response=RunnerOut,
            summary="Choose the session runtime a laptop runner opens new sessions in")
def set_runner_engine(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerEngineIn):
    """Set the runtime (`emdash`, `claude-desktop` or `claude-cli`) a laptop runner
    opens new sessions in. Takes effect on the runner's next heartbeat; sessions already
    running stay where they are."""
    # canopy-web#1188. The runner reads `engine` off its heartbeat response, so a
    # flip needs no restart and no shell on the box, and is safe while sessions are
    # live: a thread already running in one runtime keeps its own session, and
    # routing is the same either way. Owner or runner admins, like flags — it
    # changes how the box works, not whose memberships it speaks with. A cloud
    # runner has one runtime (Claude headless) and refuses rather than ignoring it.
    runner = runner_admin_or_404(request, runner_id)
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
    out.can_administer = harness_runners.can_administer_runner(request.user, out)
    return _with_roots(request, out)


def _open_sessions_by_runner(runner_ids) -> dict:
    """{runner id: sessions open on it} — active and still reported, the session
    list's own `state=active` rule (`unseen_q`), so the two counts cannot drift."""
    from apps.canopy_sessions.staleness import active

    if not runner_ids:
        return {}
    rows = (
        active(Session.objects.filter(runner_binding__runner_id__in=runner_ids))
        .values("runner_binding__runner_id")
        .annotate(n=models.Count("pk", distinct=True))
    )
    return {row["runner_binding__runner_id"]: row["n"] for row in rows}


def _with_roots(request: HttpRequest, runners):
    """Stamp each RunnerOut row's `roots` for this caller — the ONE helper every
    route returning a RunnerOut passes its runner(s) through (the schema serves
    [] for an unstamped row, so a route that forgets reveals nothing).

    The owner sees every tree the box is enrolled in. Anyone else sees a tree's
    root only when they are a member somewhere in that tree (a member of only
    `connect` sees `dimagi` for a box serving `connect`). One tree walk for all
    the rows; it also fills the cache `spans_trees` reads."""
    from .routing import enrolled_slugs

    rows = [runners] if isinstance(runners, Runner) else list(runners)
    if not rows:
        return runners
    enrolled = {r.pk: enrolled_slugs(r) for r in rows}
    mine = wsvc.user_workspace_slugs(request.user)
    roots = wsvc.tree_roots(set().union(*enrolled.values()) | mine)
    my_roots = {roots[s] for s in mine}
    for r in rows:
        all_roots = sorted({roots[s] for s in enrolled[r.pk]})
        r._enrolled_roots_cache = all_roots
        r.visible_roots = (all_roots if r.owner_id == request.user.id
                           else [root for root in all_roots if root in my_roots])
    return runners


@router.get("/runners/", response=list[RunnerOut], summary="List the fleet I can see")
def list_runners(request: HttpRequest):
    """The supervisor's runner status, and the fleet read every preflight makes.

    Scoped by TENANT (`runner_read_q`), not by who owns what: a member who
    paired nothing used to list nothing, which reads identically to "this
    workspace has no runners" and is the wrong answer to draw a conclusion from.
    Each row carries `can_manage` for the ownership half. Retired runners are
    excluded at lookup, as everywhere else.
    """
    qs = (
        Runner.objects.exclude(status=Runner.RETIRED)
        .filter(runner_read_q(request))
        .prefetch_related("declared_flags", "enrollments")
        .order_by(models.F("last_heartbeat_at").desc(nulls_last=True))
    )
    from apps.embedding import delegation

    offered = delegation.offered_for(request)
    if offered is not None:
        # A site sees only the runners that serve its own agents — enough for a
        # "continue on…" picker, and nothing about the rest of the fleet.
        following = {r.pk for r in qs if routing.agents_following_runner(r) & set(offered)}
        qs = qs.filter(Q(agent_assignments__agent_id__in=offered) | Q(pk__in=following)).distinct()
    rows = list(qs[:50])
    open_by_runner = _open_sessions_by_runner([r.pk for r in rows])
    for r in rows:
        r.open_sessions = open_by_runner.get(r.pk, 0)
        # Resolved here rather than in the schema because it is a property of the
        # (caller, runner) PAIR, and a Ninja resolver only sees the row.
        r.can_manage = r.owner_id in (request.user.id, None)
        # Administration is a WIDER tier than acting as the runner, so it gets
        # its own flag rather than overloading can_manage — the credentials block
        # and the drill panel are gated by different routes.
        r.can_administer = harness_runners.can_administer_runner(request.user, r)
    return _with_roots(request, rows)


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
    runner = runner_or_404(request, runner_id)
    if "projects" in payload.capabilities:
        raise HttpError(
            422,
            "`projects` is reported by the runner, not set by hand — it is replaced "
            "on every heartbeat from what the box actually has. To make a repo "
            "routable, open it as a project in emdash on that runner (or set "
            "RUNNER_PROJECTS on a cloud runner). PATCH `agents`/`sessions` freely.",
        )
    for key in ("profiles", "envelope", "midturn", "unprepared", "data_mode", "data_mode_hash"):
        if key in payload.capabilities:
            # Reported, like `projects` — and here it is a SECURITY property: a hand
            # edit claiming a runner can confine a caller's session would route
            # confined turns to a box that runs them in the full profile.
            raise HttpError(422, f"`{key}` is reported by the runner on every heartbeat, "
                                 "not set by hand.")
    caps = dict(payload.capabilities)
    for key in ("projects", "profiles", "envelope", "midturn", "unprepared", "data_mode", "data_mode_hash"):
        if runner.capabilities.get(key) is not None:
            caps[key] = runner.capabilities[key]
    # The isolation unit is SERVER-OWNED once it is set (`unit_pairing` records it
    # from the invitation a person approved): a write can neither replace nor clear
    # it, or an agent unit could lift its own restriction. A box-sent value is
    # ignored; a runner with none may still declare one (a self-restriction).
    if runner.capabilities.get("unit"):
        caps["unit"] = runner.capabilities["unit"]
    runner.capabilities = caps
    runner.save(update_fields=["capabilities"])
    return _with_roots(request, runner)


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
    runner = runner_or_404(request, runner_id)
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
    # Turns pinned to it would wait for it forever: nothing else may take a pin.
    for turn in Turn.objects.filter(status=Turn.QUEUED, pinned_runner=runner):
        services.finish_turn(turn, status=Turn.CANCELLED, allow_queued=True,
                             result_note=f"cancelled: {runner.name}, the runner it was pinned to, was retired")
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
    runner = runner_or_404(request, runner_id, include_retired=True)
    if runner.status == Runner.RETIRED:
        runner.status = Runner.DISCONNECTED
        runner.save(update_fields=["status"])
    return _with_roots(request, runner)


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

    `until` also schedules the unpause (`Runner.unpause_at`) — what a runner sends
    when its Claude subscription hits a usage cap, with the reset the CLI named,
    so the box is parked exactly as long as it cannot work and the server's sweep
    (`services.wake_due_runners`) lifts it. A scheduled unpause never weakens a
    stronger pause: it is not added to an operator's open-ended pause, and a
    second cap only ever moves it later. A pause without `until` cancels any
    scheduled unpause — the operator outranks the clock.
    """
    runner = runner_or_404(request, runner_id)
    services.pause(runner, note=payload.note or "", until=payload.until, by=request.user)
    return _with_roots(request, runner)


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
    runner = runner_or_404(request, runner_id)
    services.unpause(runner)
    return _with_roots(request, runner)


def _may_release_fleet_hold(user) -> bool:
    return bool(getattr(user, "is_superuser", False))


def _may_hold_fleet(user) -> bool:
    """A superuser, or a named holder (`CANOPY_FLEET_HOLDERS` — Ada). Holding is the
    safe direction: it starts nothing and loses nothing, so the fleet's conductor may
    pull it unattended. Releasing is not widened (see `_may_release_fleet_hold`)."""
    if _may_release_fleet_hold(user):
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    holders = {e.strip().lower() for e in getattr(settings, "CANOPY_FLEET_HOLDERS", []) if e.strip()}
    return bool(email) and email in holders


def _fleet_hold_out(hold: FleetHold, user) -> dict:
    return {
        "can_hold": _may_hold_fleet(user),
        "can_release": _may_release_fleet_hold(user),
        "held": hold.held, "note": hold.note, "held_at": hold.held_at,
        "held_by_email": (hold.held_by.email if hold.held_by_id else ""),
        "queued": Turn.objects.filter(status=Turn.QUEUED).count() if hold.held else 0,
    }


@router.get("/fleet-hold", response=FleetHoldOut)
def get_fleet_hold(request: HttpRequest):
    """Is the whole fleet on hold? Readable by anyone signed in — a member whose turn
    is sitting queued should be able to see why."""
    return _fleet_hold_out(FleetHold.current(), request.user)


@router.post("/fleet-hold", response=FleetHoldOut)
def hold_fleet(request: HttpRequest, payload: FleetHoldIn):
    """Stop EVERY runner from starting anything — the fleet-wide sibling of
    /runners/{id}/pause (see FleetHold). Turns keep enqueuing and wait QUEUED with
    their trigger, so `list turns?status=queued` shows what tried to start while
    held. Running turns finish normally. A superuser or a named holder
    (`CANOPY_FLEET_HOLDERS`, i.e. Ada) — it spans every tenant, but only stops work.
    Idempotent — holding again refreshes the note."""
    if not _may_hold_fleet(request.user):
        raise HttpError(403, "holding the whole fleet requires a superuser or a named fleet holder")
    hold = FleetHold.current()
    if not hold.held:
        hold.held_at = timezone.now()
        hold.held_by = request.user
    hold.held = True
    hold.note = (payload.note or "")[:500]
    hold.save()
    return _fleet_hold_out(hold, request.user)


@router.post("/fleet-hold/release", response=FleetHoldOut)
def release_fleet_hold(request: HttpRequest):
    """Release the fleet hold; queued turns become claimable again. Wakes every
    tenant's runners so the backlog starts now rather than at each box's next poll.
    Superuser only — a named holder may stop the fleet, never restart it."""
    if not _may_release_fleet_hold(request.user):
        raise HttpError(403, "releasing the fleet hold requires a superuser")
    hold = FleetHold.current()
    if hold.held:
        hold.held, hold.note, hold.held_at, hold.held_by = False, "", None, None
        hold.save()
        from apps.realtime import groups

        slugs = list(Workspace.objects.values_list("slug", flat=True))
        transaction.on_commit(lambda: [
            groups.publish(groups.runnable_group(slug), {"type": "runner.wake"})
            for slug in slugs
        ])
    return _fleet_hold_out(hold, request.user)


@router.post("/runners/{runner_id}/heartbeat", response=RunnerOut)
def runner_heartbeat(request: HttpRequest, runner_id: uuid.UUID, payload: HeartbeatIn):
    from . import runner_protocol
    from .api_device_keys import waiting_approval_url

    runner = runner_or_404(request, runner_id)
    beat = runner_protocol.heartbeat(runner, payload)
    beat.key_waiting_url = waiting_approval_url(runner)
    return beat


@router.post("/runners/{runner_id}/refresh", response=RunnerOut)
def refresh_runner(request: HttpRequest, runner_id: uuid.UUID):
    """Ask this runner to refresh itself at its next idle moment: re-run its
    bootstrap, which updates the canopy plugin and CLI, Claude Code, and each
    agent's provisioning, then restart. `refresh_pending` stays true until the
    runner reports a bootstrap newer than the request."""
    # The administer tier, not the act-as tier: this changes what the box runs
    # on, the same class of operation as setting its credentials.
    runner = runner_admin_or_404(request, runner_id)
    return _with_roots(request, harness_runners.request_refresh(runner))


@router.get("/runners/{runner_id}/usage-scope", response=UsageScopeOut)
def runner_usage_scope(request: HttpRequest, runner_id: uuid.UUID):
    """The repos this box scans for its usage report (its declared projects and the
    agents it serves), and whether a person asked for a full rescan."""
    from apps.events import usage

    runner = runner_or_404(request, runner_id)
    return UsageScopeOut(repos=usage.scope(runner), full=runner.usage_scan_requested_at is not None)


@router.post("/runners/{runner_id}/usage", response=UsageReportOut)
def post_runner_usage(request: HttpRequest, runner_id: uuid.UUID, payload: UsageReportIn):
    """The box's usage report: per Claude Code session, per UTC day, tokens and a
    little identity — never content. Each session's days replace what was held.
    Matched to canopy's own record of each session; one that matches nothing is
    kept as missing, visible only to the box's admins."""
    from apps.events import usage

    runner = runner_or_404(request, runner_id)
    return UsageReportOut(**usage.record(runner, [s.model_dump() for s in payload.sessions], full=payload.full))


@router.post("/runners/{runner_id}/usage-scan", response=MissingSessionsOut)
def request_usage_scan(request: HttpRequest, runner_id: uuid.UUID):
    """Check for missing sessions: ask the box for a FULL rescan of its transcripts
    (a durable ask it reads off `usage-scope` within a minute or so); the result
    arrives as its next report. The runner's admins."""
    from django.utils import timezone

    runner = runner_admin_or_404(request, runner_id)
    Runner.objects.filter(pk=runner.pk).update(usage_scan_requested_at=timezone.now())
    runner.refresh_from_db()
    return _missing_sessions(runner)


@router.get("/runners/{runner_id}/missing-sessions", response=MissingSessionsOut)
def list_missing_sessions(request: HttpRequest, runner_id: uuid.UUID):
    """Sessions in known repos this box has on disk and canopy has no record of,
    newest first, each with where sending it up would file it. The runner's
    admins only: titles of a person's local sessions are theirs."""
    return _missing_sessions(runner_admin_or_404(request, runner_id))


@router.post("/runners/{runner_id}/missing-sessions", response=SendUpOut)
def send_up_missing_sessions(request: HttpRequest, runner_id: uuid.UUID, payload: SendUpIn):
    """Send the chosen missing sessions up: each is filed as a session (where the
    report sweep files this box's sessions) and its transcript is requested from
    the box; its usage counts from then on. The runner's admins."""
    from apps.events import usage

    runner = runner_admin_or_404(request, runner_id)
    return SendUpOut(sessions=usage.send_up(runner, payload.claude_sessions))


def _missing_sessions(runner) -> MissingSessionsOut:
    from apps.events import usage

    from .services import session_home

    rows = []
    for r in usage.missing(runner):
        filed = session_home(runner, r["repo"])
        if filed is None:
            continue  # another tree's agent's work: this box never files it
        agent, _project, ws = filed
        rows.append({**r, "files_in": getattr(ws, "slug", "") or "", "agent": agent.slug if agent else ""})
    return MissingSessionsOut(
        runner=runner.name, usage_reported_at=runner.usage_reported_at,
        usage_scan_requested_at=runner.usage_scan_requested_at, usage_scanned_at=runner.usage_scanned_at,
        sessions=rows)


@router.post("/runners/{runner_id}/claim", response={200: ClaimedTurnOut, 204: None})
def claim_turn(request: HttpRequest, runner_id: uuid.UUID, paused: str = ""):
    """Claim the next eligible turn. `paused` is an optional comma-separated list of
    agent slugs the caller has locally paused (per-agent pause) — the server skips
    their queued turns so nothing is claimed-then-released. Omitted by older runners
    (backward-compatible: no exclusions). `X-Canopy-Acts-As: editor|viewer` claims in
    that lower mode: a turn the runner's owner started runs as that role."""
    runner = runner_or_404(request, runner_id)
    exclude = [s for s in (p.strip() for p in paused.split(",")) if s]
    from apps.workspaces import role_cap

    # A runner may claim in a lower mode it put itself in (X-Canopy-Acts-As).
    turn = claim.claim_next_turn(runner, exclude_slugs=exclude or None,
                                    acts_as=role_cap.header_cap(request))
    if turn is None:
        return Status(204, None)
    # The same credentials the WebSocket claim carries — see `claiming.py`,
    # which exists because the two channels once disagreed about them.
    from .claiming import issue_credentials

    return Status(200, issue_credentials(turn))
