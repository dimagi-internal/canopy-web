"""Site sign-offs' doors (`sign_offs.py`).

A connected site, signed with its own key: `POST /api/sites/sign-offs` asks, and
`GET /api/sites/sign-offs/{id}` reads one request back. The approver, in canopy's web
app: `GET /api/sign-offs/{id}` shows it, `POST .../approve` (a browser, a person, a
passkey step-up) and `POST .../decline` decide it.
"""
from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest, JsonResponse
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.embedding.audit import client_ip
from apps.embedding.rate_limit import (
    SiteAgentsRateLimitError,
    check_site_agents_client,
    check_site_agents_issuer,
)
from apps.passkeys import stepup
from apps.tokens.models import SiteSignOff

from . import client_identity, site_agents
from . import sign_offs as service
from .api import _bearer

sites_router = Router(tags=["connected apps"])
router = Router(tags=["connected apps"])


def _verified(request: HttpRequest, *, sign_off_id: str | None = None):
    # Budgets in the order the work happens: per client before anything is parsed,
    # per site before its signature is checked (the site agents API's).
    try:
        check_site_agents_client(client_ip(request))
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    token = _bearer(request)
    try:
        claimed = site_agents.site_for_request(token)
        check_site_agents_issuer(f"{claimed.workspace_id}:{claimed.name}")
        return service.verify_request(token, sign_off_id=sign_off_id)
    except SiteAgentsRateLimitError as exc:
        raise HttpError(429, f"rate_limited: {exc}")
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")


class SignOffCreatedOut(Schema):
    id: str
    approve_url: str
    expires_at: datetime
    #: `subject` (the subject approves their own) or `agent_admins` (the agent's owner
    #: or an admin approves).
    approvers: str


@sites_router.post("/", auth=None, response={201: SignOffCreatedOut},
                   summary="A connected site asks a person to approve one action, signed")
@gate("host", kind="request")  # auth=None; a request signed by the site's own registered key IS the credential
def create_site_sign_off(request: HttpRequest):
    """Ask for a person's passkey approval of ONE action on this site.

    `Authorization: Bearer <JWT>` signed by the site's own key: `typ`
    `canopy-sign-off-request+jwt`, `iss` = `sub` = its Connected-site name, `aud` = this
    URL, at most 60 s, single-use `jti`, `canopy_workspace` / `canopy_agent` as for
    `GET /api/sites/agents`, and `sign_off_request`: `subject` (the account's email),
    `action` (`key`, `title`, `detail`), `site_ref` (the site's id for what waits on
    this) and `return_url` (https, on an origin the site registered; else 422
    `bad_return_url`). Build and send it with `canopy_sdk.sign_offs.request_sign_off`.
    Answers 201 with the page to send the approver to; 422 `no_approver` when nobody in
    canopy may approve for that subject. A person's own request lasts 15 minutes, an
    agent's 24 hours.
    """
    app, claims = _verified(request)
    try:
        row = service.create(app, claims["sign_off_request"])
    except site_agents.SiteAgentsRefused as exc:
        raise HttpError(exc.status, f"{exc.code}: {exc.message}")
    return 201, {"id": row.id, "approve_url": service.approve_url(row), "expires_at": row.expires_at,
                 "approvers": row.approvers}


@sites_router.get("/{sign_off_id}", auth=None, summary="A connected site reads one of its sign-off requests, signed")
@gate("host")  # auth=None; a request signed by the site's own registered key IS the credential
def get_site_sign_off(request: HttpRequest, sign_off_id: str):
    """The state of one request this site made: `{id, state, expires_at, approval}`,
    `state` one of `open`, `approved`, `declined`, `expired`. `approval` is the signed
    approval (verify it with `canopy_sdk.sign_offs.verify_approval`) for ten minutes
    after it was given, else null. Signed like the ask, with `aud` = this URL and no
    `sign_off_request`. Another site's request is 404."""
    app, _claims = _verified(request, sign_off_id=sign_off_id)
    row = SiteSignOff.objects.select_related("app", "decided_by").filter(pk=sign_off_id, app=app).first()
    if row is None:
        raise HttpError(404, "not_found: no such sign-off request from this site")
    try:
        approval = service.approval_for(row)
    except client_identity.ClientIdentityError:
        raise HttpError(503, "not_configured: this canopy has no client key to sign with")
    response = JsonResponse({"id": row.id, "state": row.state_now(), "expires_at": row.expires_at.isoformat(),
                             "approval": approval})
    response["Cache-Control"] = "no-store"
    return response


# --- the approver ---------------------------------------------------------------------


class SignOffOut(Schema):
    id: str
    site: str
    workspace: str
    subject: str
    action_key: str
    action_title: str
    action_detail: str
    #: `subject` or `agent_admins`.
    approvers: str
    agent_slug: str | None = None
    agent_name: str | None = None
    #: `open`, `approved`, `declined` or `expired`.
    state: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by_email: str | None = None


class SignOffDecidedOut(SignOffOut):
    #: Where to send the browser now: the site's page, with the approval (or the
    #: decline) in its query.
    redirect_url: str


def _out(row: SiteSignOff) -> dict:
    return {
        "id": row.id, "site": row.app.name, "workspace": row.app.workspace_id, "subject": row.subject,
        "action_key": row.action_key, "action_title": row.action_title, "action_detail": row.action_detail,
        "approvers": row.approvers, "agent_slug": row.agent.slug if row.agent else None,
        "agent_name": row.agent.name if row.agent else None, "state": row.state_now(),
        "created_at": row.created_at, "expires_at": row.expires_at, "decided_at": row.decided_at,
        "decided_by_email": row.decided_by.email if row.decided_by else None,
    }


def _mine(request: HttpRequest, sign_off_id: str) -> SiteSignOff:
    """The request, if the caller may decide it. Anyone else gets the same 404 as a
    request that does not exist."""
    row = (SiteSignOff.objects.select_related("app", "agent", "decided_by")
           .filter(pk=sign_off_id).first())
    if row is None or not service.may_approve(row, request.user):
        raise HttpError(404, "not_found: no such sign-off request")
    return row


@router.get("/{sign_off_id}", response=SignOffOut, summary="A sign-off request you may decide")
@gate("sign-off-approver")
def get_sign_off(request: HttpRequest, sign_off_id: str) -> SignOffOut:
    """What a connected site asks you to approve: the site, the action in its own
    words, the account it is for, and (for an agent's request) which agent. 404 unless
    you may decide it."""
    return SignOffOut(**_out(_mine(request, sign_off_id)))


@router.post("/{sign_off_id}/approve", response=SignOffDecidedOut,
             summary="Approve a site's sign-off request (browser, passkey)")
@gate("sign-off-approver", setting=True, step_up=True)
def approve_sign_off(request: HttpRequest, sign_off_id: str) -> SignOffDecidedOut:
    """Approve, in your own browser after a passkey step-up: canopy signs the approval
    and answers where to send the browser (the site's page, approval in its query). A
    token is refused (403), so no agent or script can approve. 409 `closed` when it was
    decided already or has expired."""
    row = _mine(request, sign_off_id)
    stepup.require_recent_step_up(request)
    try:
        row = service.approve(row, request.user)
        approval = service.approval_for(row)
    except service.SignOffClosedError as exc:
        raise HttpError(409, f"closed: this request is {exc}")
    except client_identity.ClientIdentityError:
        raise HttpError(503, "not_configured: this canopy has no client key to sign with")
    return SignOffDecidedOut(**_out(row), redirect_url=service.redirect_url(row, approval))


@router.post("/{sign_off_id}/decline", response=SignOffDecidedOut, summary="Decline a site's sign-off request")
@gate("sign-off-approver", setting=True)
def decline_sign_off(request: HttpRequest, sign_off_id: str) -> SignOffDecidedOut:
    """Decline; the site is told when it next reads the request, and the answer says
    where to send the browser. 409 `closed` when it was decided already or has expired."""
    row = _mine(request, sign_off_id)
    try:
        row = service.decline(row, request.user)
    except service.SignOffClosedError as exc:
        raise HttpError(409, f"closed: this request is {exc}")
    return SignOffDecidedOut(**_out(row), redirect_url=service.redirect_url(row, None))
