"""/api/harness runner routes: a runner's Claude sign-in (mint, Google mint, sign-in requests).
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Status
from ninja.errors import HttpError

from apps.agents.credentials import google_mint as agent_google_mint
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.events import audit as events_audit

from . import runners as harness_runners
from .api import (  # noqa: E402
    router,
    runner_admin_or_404,
    runner_or_404,
)
from .api_runners import _list_admin_or_404
from .schemas import (
    GoogleMintChallengeIn,
    GoogleMintResultIn,
    OpenSignInRequestOut,
    RunnerMintClaimOut,
    RunnerMintCodeIn,
    RunnerMintEntryIn,
    RunnerMintOut,
    RunnerMintResultIn,
    RunnerMintStartIn,
    RunnerMintUrlIn,
    SignInRequestIn,
    SignInRequestOut,
    SignInResultIn,
    SignInResultOut,
)


@router.post("/runners/{runner_id}/mint", response=RunnerMintOut,
             summary="Ask a runner to start a browser sign-in")
@gate("runner-admin", kind="action")
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
    - `token`: a `claude setup-token` for the credential list — a new token at
      the end, or (`login_key`) a new value for that token. Inference only, but
      good for a year with nothing to renew. `primary`/`secondary` are the old
      fixed slots: the first and second token.
    """
    runner = _list_admin_or_404(request, runner_id)
    slot = payload.slot if payload else "box"
    login_key = (payload.login_key if payload else "") if slot in ("box", "token") else ""
    kind = "login" if slot == "box" else "token"
    if slot == "box" and runner.credential_set_id:
        raise HttpError(409, harness_runners.SET_LOGIN_REFUSAL)
    if login_key and not harness_runners._entries(runner).filter(key=login_key, kind=kind).exists():
        raise HttpError(404, f"no such {kind} on this runner")
    return harness_runners.start_runner_mint(runner, requested_by=request.user, slot=slot,
                                      login_key=login_key)


@router.get("/runners/{runner_id}/mint", response=RunnerMintOut | None,
            summary="The current sign-in attempt, if any")
@gate("runner-admin")
def get_runner_mint(request: HttpRequest, runner_id: uuid.UUID):
    """What the waiting human's screen renders: whether the URL is up yet, and
    how the attempt ended. Null when no sign-in has ever been started."""
    runner = runner_admin_or_404(request, runner_id)
    return harness_runners.current_runner_mint(runner)


@router.get("/runners/{runner_id}/mint/claim", response=RunnerMintClaimOut,
            summary="Work the RUNNER owes on a sign-in (polled)")
@gate("runner")
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
    google, google_code = agent_google_mint.claim(runner)
    out = {"mint": None, "code": "",
           "google": agent_google_mint.work(google, google_code) if google is not None else None}
    mint = harness_runners.claim_runner_mint(runner)
    if mint is None:
        return out
    return {**out, "mint": mint, "code": harness_runners.take_mint_code(mint)}


@router.post("/runners/{runner_id}/mint/url", response=RunnerMintOut,
             summary="The runner reports the URL a human must open")
@gate("runner", kind="runner-protocol")
def post_runner_mint_url(request: HttpRequest, runner_id: uuid.UUID,
                         payload: RunnerMintUrlIn):
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None or mint.status in mint.FINISHED:
        raise HttpError(409, "no sign-in is in progress for this runner")
    return harness_runners.record_mint_url(mint, payload.url)


@router.post("/runners/{runner_id}/mint/code", response=RunnerMintOut,
             summary="A human submits the authorization code")
@gate("runner-admin", kind="action")
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


@router.post("/runners/{runner_id}/mint/entry", response=RunnerMintOut,
             summary="The runner names the entry a token it signed in will be sealed under")
@gate("runner", kind="runner-protocol")
def post_runner_mint_entry(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerMintEntryIn):
    """`{vault_key}` — the sign-in's own entry, else a new one the box names, with no
    value here. The box then seals the token into the runner's vault under it, and
    only then reports the sign-in (`…/mint/result {vault_key}`)."""
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None:
        raise HttpError(409, "no sign-in is in progress for this runner")
    try:
        harness_runners.name_minted_entry(mint, payload.vault_key)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(422, str(exc)) from exc
    return mint


@router.post("/runners/{runner_id}/mint/result", response=RunnerMintOut,
             summary="The runner reports the outcome of a sign-in")
@gate("runner", kind="runner-protocol", step_up="runner protocol")
def post_runner_mint_result(request: HttpRequest, runner_id: uuid.UUID,
                            payload: RunnerMintResultIn):
    """How the sign-in ended. A `box` login stays on the box (`logged_in`); a `token`
    sign-in names the entry it sealed into the runner's vault (`vault_key`) and is done
    only if the vault holds it. A token itself is never accepted: one sent here fails
    the sign-in, unstored. The only secret a human handles is the single-use code."""
    runner = runner_or_404(request, runner_id)
    mint = harness_runners.current_runner_mint(runner)
    if mint is None:
        raise HttpError(409, "no sign-in is in progress for this runner")
    try:
        return harness_runners.finish_runner_mint(mint, token=payload.token, detail=payload.detail,
                                                  logged_in=payload.logged_in, account=payload.account,
                                                  vault_key=payload.vault_key)
    except harness_runners.CredentialRefused as exc:
        raise HttpError(422, str(exc)) from exc


def _google_mint_problem(exc) -> ProblemError:
    return ProblemError(exc.status, "Google sign-in", detail=exc.detail, members={"error": exc.code})


@router.post("/runners/{runner_id}/google-mint/{mint_id}/challenge", response={204: None},
             summary="The runner posts the PKCE challenge for an agent's Google sign-in")
@gate("runner", kind="runner-protocol")
def post_google_mint_challenge(request: HttpRequest, runner_id: uuid.UUID, mint_id: uuid.UUID,
                               payload: GoogleMintChallengeIn):
    """The box made a verifier and keeps it; canopy-web builds the consent link from
    the challenge, so the code Google later sends here cannot be redeemed without the box."""
    runner = runner_or_404(request, runner_id)
    try:
        agent_google_mint.record_challenge(runner, mint_id, payload.code_challenge)
    except agent_google_mint.MintRefused as exc:
        raise _google_mint_problem(exc) from exc
    return Status(204, None)


@router.post("/runners/{runner_id}/google-mint/{mint_id}/result", response={204: None},
             summary="The runner reports an agent's Google sign-in: ciphertext only")
@gate("runner", setting=True, step_up="runner protocol: the box's own token, and ciphertext only")
def post_google_mint_result(request: HttpRequest, runner_id: uuid.UUID, mint_id: uuid.UUID,
                            payload: GoogleMintResultIn):
    """`{value_zk, kv, account, name}` — the agent's `gog-token`, encrypted on the box for the
    agent's readers (or a person's own account, sealed to the machines that may hold it,
    under the work's `name`) — or `{error, detail}`. A value that is not `zk1`/`zks1` ciphertext,
    or is sealed to someone who may not read the agent, is refused; a token for another
    Google account than the agent's is not stored."""
    runner = runner_or_404(request, runner_id)
    try:
        if payload.error or not payload.value_zk:
            agent_google_mint.runner_failed(runner, mint_id, outcome=payload.error or "exchange-failed",
                                            detail=payload.detail, account=payload.account)
            return Status(204, None)
        mint = agent_google_mint.finish(runner, mint_id, kv=payload.kv, value_zk=payload.value_zk,
                                        account=payload.account, name=payload.name)
    except agent_google_mint.MintRefused as exc:
        raise _google_mint_problem(exc) from exc
    if mint.status == mint.DONE and mint.delegated_by_id:
        name = agent_google_mint.delegated_entry_name(mint.expected_account)
        events_audit.note_change(scope=f"agent:{mint.agent.slug}:credential:{name}", before=None,
                                 after={"google_account": mint.account, "delegated_by": mint.delegated_by.email},
                                 workspace=mint.agent.workspace,
                                 summary=f"{mint.delegated_by.email} delegated their Google account "
                                         f"({mint.account}) to {mint.agent.slug} via {runner.name}")
    elif mint.status == mint.DONE:
        events_audit.note_change(scope=f"agent:{mint.agent.slug}:credential:gog-token", before=None,
                                 after={"google_account": mint.account}, workspace=mint.agent.workspace,
                                 summary=f"connected {mint.agent.slug}'s Google account ({mint.account}) "
                                         f"via {runner.name}")
    return Status(204, None)


@router.post("/runners/{runner_id}/sign-in-request", response=SignInRequestOut,
             summary="Ask the runner's owner to approve a cloud sign-in from their phone")
@gate("runner", kind="runner-protocol")  # pushes only to the runner's owner
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
@gate("runner", kind="runner-protocol")
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
@gate("self")  # only requests put in front of the caller
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
