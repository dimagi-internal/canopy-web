from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.errors import TYPE_CONFLICT, TYPE_RATE_LIMIT, ProblemError
from apps.tokens.audit import client_ip
from apps.workspaces import permissions as perms
from apps.workspaces import services as ws_services
from apps.workspaces.models import Workspace

from . import services
from .models import BetaRequest
from .schemas import BetaRequestDetailOut, BetaRequestIn, BetaRequestInviteIn, BetaRequestOut

router = Router(tags=["beta"])


# `auth=None` is half the story: apps/common/middleware.py is default-deny, so this
# path is also allowlisted there. The response is identical for a first request and
# a repeat, so the form cannot be used to learn who has already asked.
@router.post("/beta-requests", response=BetaRequestOut, auth=None,
             summary="Request access to Canopy (anonymous)")
def submit_beta_request(request: HttpRequest, payload: BetaRequestIn) -> dict:
    """Ask for access to canopy, which is currently closed to Dimagi and its partners.

    Records the request and emails the person who grants access a link to the
    request's page, where they invite the asker to a workspace or decline.
    """
    if payload.website:
        return {"ok": True}
    try:
        services.submit(
            email=str(payload.email), reason=payload.reason,
            client_ip=client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except services.TooManyRequests:
        raise ProblemError(429, "Too many requests", type_=TYPE_RATE_LIMIT,
                           detail="Too many requests from this address. Try again later.")
    return {"ok": True}


def _out(req: BetaRequest, email_status: str | None = None) -> BetaRequestDetailOut:
    return BetaRequestDetailOut(
        id=req.pk, email=req.email, reason=req.reason, created_at=req.created_at,
        status=req.status, workspace=req.workspace_id,
        workspace_name=req.workspace.display_name if req.workspace else None,
        role=req.role, decided_by=req.decided_by.email if req.decided_by else None,
        decided_at=req.decided_at, email_status=email_status,
    )


def _review_or_404(request: HttpRequest, request_id: int | None = None) -> BetaRequest | None:
    """404 for anyone who is not a reviewer (`services.may_review`), so the
    route cannot be used to learn that a request exists."""
    if not services.may_review(request.user):
        raise HttpError(404, "not found")
    if request_id is None:
        return None
    req = BetaRequest.objects.select_related("workspace", "decided_by").filter(pk=request_id).first()
    if req is None:
        raise HttpError(404, "not found")
    return req


@router.get("/beta-requests", response=list[BetaRequestDetailOut],
            summary="List requests for access to Canopy (reviewers)")
def list_beta_requests(request: HttpRequest, status: str | None = None) -> list[BetaRequestDetailOut]:
    """Newest first; `status` filters to pending, invited or declined. Only the
    person requests are mailed to, and superusers, may read them."""
    _review_or_404(request)
    qs = BetaRequest.objects.select_related("workspace", "decided_by")
    if status:
        qs = qs.filter(status=status)
    return [_out(r) for r in qs[:200]]


@router.get("/beta-requests/{request_id}", response=BetaRequestDetailOut,
            summary="One request for access to Canopy (reviewers)")
def get_beta_request(request: HttpRequest, request_id: int) -> BetaRequestDetailOut:
    return _out(_review_or_404(request, request_id))


@router.post("/beta-requests/{request_id}/invite", response=BetaRequestDetailOut,
             summary="Approve: invite the requester to a workspace (reviewers)")
def invite_beta_request(request: HttpRequest, request_id: int,
                        payload: BetaRequestInviteIn) -> BetaRequestDetailOut:
    """Invites the requester to `workspace` at `role` and emails them the link —
    the same invite as Settings → Members, so the caller needs `members.manage`
    there and may grant only below their own role (owners: any).
    `email_status` says whether the invite email went out. 409 if the request
    was already answered."""
    req = _review_or_404(request, request_id)
    role = ws_services.member_role(request.user, payload.workspace)
    ws = Workspace.objects.filter(slug=payload.workspace).first()
    if ws is None or role is None:
        raise HttpError(404, "workspace not found")
    if not perms.role_allows(role, perms.MEMBERS_MANAGE):
        raise HttpError(403, "inviting needs the admin role or above in that workspace")
    if not perms.may_manage_member(role, None, payload.role):
        raise HttpError(403, "you can only grant roles below your own")
    try:
        req, email_status = services.invite(req, workspace=ws, role=payload.role, by=request.user)
    except services.NotPending:
        raise ProblemError(409, "Already answered", type_=TYPE_CONFLICT,
                           detail="This request was already answered.")
    return _out(req, email_status)


@router.post("/beta-requests/{request_id}/decline", response=BetaRequestDetailOut,
             summary="Decline a request for access to Canopy (reviewers)")
def decline_beta_request(request: HttpRequest, request_id: int) -> BetaRequestDetailOut:
    """Closes the request. Emails nobody — reply to the notification email to
    tell them why. 409 if it was already answered."""
    req = _review_or_404(request, request_id)
    try:
        return _out(services.decline(req, by=request.user))
    except services.NotPending:
        raise ProblemError(409, "Already answered", type_=TYPE_CONFLICT,
                           detail="This request was already answered.")
