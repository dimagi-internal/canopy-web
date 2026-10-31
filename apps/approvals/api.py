"""Django Ninja router for /api/approvals — typed requests for a person's yes or no.

A request names one `kind` from a fixed registry (`GET /api/approvals/kinds`) with typed params;
the server renders what approving will do from those params, and approving runs the kind's
hard-coded handler as the approver. No model is involved in approving.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.gates import visible_agent_workspaces
from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms

from . import authority, cards, kinds, services
from .models import ApprovalRequest
from .schemas import ApprovalCardOut, ApprovalDecideIn, ApprovalFileIn, ApprovalKindOut

router = Router(auth=session_auth, tags=["approvals"])

LIST_MAX = 100


def _visible(request: HttpRequest, pk: int) -> ApprovalRequest:
    req = (ApprovalRequest.objects.select_related("workspace", "requested_by_agent", "requested_by_user",
                                                  "decided_by").filter(pk=pk).first())
    if req is None or req.workspace_id not in visible_agent_workspaces(request):
        raise HttpError(404, f"approval request {pk} not found")
    return req


def _raise(exc: services.ApprovalError):
    raise HttpError(exc.status, str(exc)) from exc


@router.get("/kinds", response=list[ApprovalKindOut], summary="The kinds of request that can be filed")
@gate("authenticated")
def list_approval_kinds(request: HttpRequest) -> list[dict]:
    """Every kind: its key, who can approve it, what the handler does, and the JSON schema of its
    params. A request must name one of these; there is no free-form request."""
    return kinds.catalog()


@router.get("/", response=list[ApprovalCardOut], summary="Requests waiting on you")
# lists only requests in the caller's own workspaces, and only those they hold the authority to approve
@gate("member")
def list_approvals(request: HttpRequest, status: str = "pending", scope: str = "") -> list[dict]:
    """Requests in your workspaces that YOU could approve. `status=all` also lists decided ones you
    could have approved; `scope` narrows to one (`workspace:dimagi`, `agent:hal`)."""
    qs = (ApprovalRequest.objects.filter(workspace_id__in=visible_agent_workspaces(request))
          .select_related("workspace", "requested_by_agent", "requested_by_user", "decided_by")
          .order_by("-created_at")[:400])
    out = []
    for req in qs:
        if status != "all" and not req.is_pending:
            continue
        if scope and scope not in req.scopes:
            continue
        if authority.may_approve(req, request.user):
            out.append(cards.build(req, viewer=request.user))
        if len(out) >= LIST_MAX:
            break
    return out


@router.post("/", response={201: ApprovalCardOut, 200: ApprovalCardOut}, summary="File a request")
# an editor of the request's workspace, or the agent's own login — the same tier that can start an agent's work
@gate("agent.work", kind="request")
def file_approval(request: HttpRequest, payload: ApprovalFileIn):
    """File a request of one kind. 422 says what is wrong with the kind or params, or that nobody can
    approve it. Replaying `idempotency_key` returns the original (200). The preview people read is
    rendered from `params`; your `reason` and `evidence` are shown as yours."""
    agent = Agent.objects.filter(user=request.user).first()
    try:
        kind, params = kinds.parse(payload.kind, payload.params)
        workspace = kind.describe(params).workspace
    except kinds.KindError as exc:
        raise HttpError(422, str(exc)) from exc
    # Filing is working in the request's workspace: an editor there, or the agent's own login. A
    # workspace the caller is not in looks like it is not there.
    if workspace not in visible_agent_workspaces(request) or not perms.can(
            request.user, workspace, perms.AGENT_WORK):
        raise HttpError(404, f"workspace {workspace!r} not found")
    try:
        req, created = services.create(
            kind=payload.kind, params=payload.params, requested_by_agent=agent,
            requested_by_user=None if agent else request.user, reason=payload.reason,
            evidence=[e.model_dump() for e in payload.evidence], source_url=payload.source_url,
            for_email=payload.for_email, expires_in_hours=payload.expires_in_hours,
            idempotency_key=payload.idempotency_key or None)
    except services.ApprovalError as exc:
        _raise(exc)
    return (201 if created else 200), cards.build(req, viewer=request.user)


@router.get("/{approval_id}", response=ApprovalCardOut, summary="One request, with everything needed to decide it")
@gate("member")
def get_approval(request: HttpRequest, approval_id: int) -> dict:
    """The server-rendered preview of what approving will do, the requester's own words apart from it,
    who it was sent to and how, and — once decided — who decided it, through which door, and what
    happened. 404 for a request in a workspace you are not in."""
    return cards.build(_visible(request, approval_id), viewer=request.user)


@router.post("/{approval_id}/decide", response=ApprovalCardOut, summary="Approve or decline a request")
# the route takes any member; the kind's own authority check (members.manage, agent owner, …) decides
@gate("member", setting=True, viewer_write="The route accepts any member of the workspace, but "
      "services.decide refuses anyone who does not hold the authority the request's kind needs "
      "(e.g. members.manage for adding a member), so a viewer is refused there")
def decide_approval(request: HttpRequest, approval_id: int, payload: ApprovalDecideIn) -> dict:
    """Approve (runs the kind's handler as you, once) or decline (nothing happens). A request is
    decided once: a second decision is a 409 naming who got there first. 403 when you do not hold
    the authority the kind needs. An approval whose handler refuses comes back `failed` with the reason."""
    _visible(request, approval_id)
    try:
        req = services.decide(approval_id, request.user, payload.decision, comment=payload.comment, via="web")
    except services.ApprovalError as exc:
        _raise(exc)
    return cards.build(_visible(request, req.pk), viewer=request.user)


@router.post("/{approval_id}/cancel", response=ApprovalCardOut, summary="Withdraw a request")
# whoever filed it, or someone who could have approved it
@gate("member", kind="request", viewer_write="Withdrawing is allowed to whoever filed the request or "
      "could have approved it; services.cancel refuses anyone else, a viewer included")
def cancel_approval(request: HttpRequest, approval_id: int) -> dict:
    _visible(request, approval_id)
    try:
        req = services.cancel(approval_id, request.user)
    except services.ApprovalError as exc:
        _raise(exc)
    return cards.build(_visible(request, req.pk), viewer=request.user)
