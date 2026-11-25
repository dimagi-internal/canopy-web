"""`/api/agents/{slug}/aws-role-requests` — an agent asks for a fenced AWS role; a person approves it.

The rules are `apps/agents/api_aws.py`'s module docstring. Asking files an approval of kind
`agent.aws_role` (`kinds/agent_aws_role.py`), approved on its page with a passkey by an owner of the
agent's workspace or a canopy superuser. The approved request is the grant `aws-credentials` reads.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError
from pydantic import Field

from apps.agents import aws_grants
from apps.agents.gates import agent_for_read, agent_for_work
from apps.agents.models import AgentAwsRoleRequest
from apps.api.auth import session_auth
from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.common.schemas import StrictModel
from apps.workspaces import permissions as perms

from . import cards, services
from .kinds.agent_aws_role import SLUG, is_agent_login

router = Router(auth=session_auth, tags=["agents"])

#: How long a request waits for a decision before it expires undecided.
PENDING_HOURS = 24
LIST_MAX = 50


class AwsRoleRequestIn(StrictModel):
    role: str = Field(max_length=64, description="A fenced role canopy-web vends, e.g. `labs-readwrite`.")
    reason: str = Field(min_length=1, max_length=2000,
                        description="Why the agent needs it now, in your own words. Shown to the approver as yours.")
    minutes: int = Field(default=AgentAwsRoleRequest.DEFAULT_MINUTES, ge=AgentAwsRoleRequest.MIN_MINUTES,
                         le=AgentAwsRoleRequest.MAX_MINUTES,
                         description="How long the grant lasts once approved (15 to 480; default 60).")


class AwsRoleRequestOut(StrictModel):
    id: int
    agent: str
    role: str
    reason: str
    minutes: int
    status: str = Field(description="pending | approved | declined | cancelled | expired | revoked")
    approval_id: int | None = None
    #: The approval's page, where an owner approves it with a passkey.
    approval_url: str = ""
    requested_by: str = ""
    approved_by: str = ""
    approved_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    revoked_by: str = ""
    revoked_at: dt.datetime | None = None
    created_at: dt.datetime


def _email(user) -> str:
    return (getattr(user, "email", "") or "") if user is not None else ""


def _out(row: AgentAwsRoleRequest) -> AwsRoleRequestOut:
    aws_grants.settle(row)
    return AwsRoleRequestOut(
        id=row.pk, agent=row.agent.slug, role=row.role, reason=row.reason, minutes=row.minutes, status=row.status,
        approval_id=row.approval_id, approval_url=cards.page_url(row.approval) if row.approval_id else "",
        requested_by=_email(row.requested_by) or f"{row.agent.slug} (its own login)",
        approved_by=_email(row.approved_by), approved_at=row.approved_at, expires_at=row.expires_at,
        revoked_by=_email(row.revoked_by), revoked_at=row.revoked_at, created_at=row.created_at)


def _owner_or_superuser(user, agent) -> bool:
    if is_agent_login(user):
        return False
    return bool(user.is_superuser) or perms.can(user, agent.workspace_id, perms.OWN)


@router.post("/{slug}/aws-role-requests", response={201: AwsRoleRequestOut, 200: AwsRoleRequestOut,
                                                     **refusals(422)},
             summary="Ask a person to let this agent take a fenced AWS role")
# the agent's own login or turn, or a member who may work the agent; a person decides it on the approval
@gate("agent.work", "agent-login", kind="request")
def request_aws_role(request: HttpRequest, slug: str, payload: AwsRoleRequestIn):
    """File a request for a role in `settings.CANOPY_AWS_ROLE_AGENTS` (a fenced role), with the
    reason and how long it should last. It files an approval (kind `agent.aws_role`) that an owner of
    the agent's workspace or a canopy superuser approves on its page (`approval_url`) with a passkey;
    then `POST aws-credentials` vends the role to this agent until the grant ends. 404 for a role
    canopy-web does not vend; 422 for a role vended on ask (none today: every role is fenced) or one
    this agent already gets with no approval. While a request for the same role is pending, asking again returns it (200)."""
    agent = agent_for_work(request, slug, allow_own_login=True,
                           forbidden=f"asking for an AWS role for {slug} needs the editor role or above, "
                                     "or the agent's own login")
    roles = aws_grants.roles()
    if payload.role not in roles:
        raise HttpError(404, f"canopy does not vend an AWS role named '{payload.role}'"
                             + (f" (it vends: {', '.join(sorted(roles))})" if roles else ""))
    standing = aws_grants.standing(payload.role)
    if standing is None:
        raise HttpError(422, f"{payload.role} is vended on ask and needs no approval: "
                             f"canopy aws credentials --role {payload.role}")
    if agent.slug in standing:
        raise HttpError(422, f"{agent.slug} already gets {payload.role} with no approval "
                             "(settings.CANOPY_AWS_ROLE_AGENTS)")
    pending = (AgentAwsRoleRequest.objects.filter(agent=agent, role=payload.role, status=AgentAwsRoleRequest.PENDING)
               .select_related("approval", "agent").first())
    if pending is not None and aws_grants.settle(pending).status == AgentAwsRoleRequest.PENDING:
        return Status(200, _out(pending))
    own = agent.is_own_login(request.user)
    try:
        with transaction.atomic():
            row = AgentAwsRoleRequest.objects.create(
                agent=agent, role=payload.role, reason=payload.reason.strip(), minutes=payload.minutes,
                requested_by=None if own else request.user)
            approval, _ = services.create(
                kind=SLUG, params={"request": row.pk}, requested_by_agent=agent if own else None,
                requested_by_user=None if own else request.user, reason=row.reason,
                expires_in_hours=PENDING_HOURS, idempotency_key=f"{SLUG}:{row.pk}")
            row.approval = approval
            row.save(update_fields=["approval"])
    except services.ApprovalError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return Status(201, _out(row))


@router.get("/{slug}/aws-role-requests", response=list[AwsRoleRequestOut],
            summary="This agent's AWS role requests and grants, newest first")
@gate("agent.work", "agent-login")
def list_aws_role_requests(request: HttpRequest, slug: str, role: str = "") -> list[AwsRoleRequestOut]:
    """The agent's requests for fenced AWS roles (optionally one `role`), newest first, each with its
    status as of now: `approved` is a live grant until `expires_at`."""
    agent = agent_for_work(request, slug, allow_own_login=True,
                           forbidden=f"{slug}'s AWS role requests need the editor role or above, "
                                     "or the agent's own login")
    qs = AgentAwsRoleRequest.objects.filter(agent=agent).select_related(
        "agent", "approval", "requested_by", "approved_by", "revoked_by")
    if role:
        qs = qs.filter(role=role)
    return [_out(row) for row in qs[:LIST_MAX]]


@router.delete("/{slug}/aws-role-requests/{request_id}", response=AwsRoleRequestOut,
               summary="Revoke a grant, or withdraw a request still waiting")
# revoking: the approver, an owner of the agent's workspace or a superuser; withdrawing: also whoever asked
@gate("member", setting=True, viewer_write="The route accepts any member who can see the agent, but only "
      "the grant's approver, an owner of the agent's workspace or a canopy superuser may revoke, and only "
      "they or whoever asked may withdraw a pending request; anyone else gets 403")
def revoke_aws_role_request(request: HttpRequest, slug: str, request_id: int) -> AwsRoleRequestOut:
    """An approved grant: revoked at once, so `aws-credentials` refuses the role from now (keys already
    handed out last until their own expiry, at most an hour). By the person who approved it, an owner
    of the agent's workspace, or a canopy superuser. A pending request: withdrawn, with its approval,
    by those people or whoever asked. 409 for one already closed."""
    agent = agent_for_read(request, slug)
    row = (AgentAwsRoleRequest.objects.select_related("agent", "approval", "requested_by", "approved_by")
           .filter(agent=agent, pk=request_id).first())
    if row is None:
        raise HttpError(404, f"AWS role request {request_id} not found")
    aws_grants.settle(row)
    user = request.user
    if row.status == AgentAwsRoleRequest.PENDING:
        asked = agent.is_own_login(user) if row.requested_by_id is None else row.requested_by_id == user.pk
        if not (asked or _owner_or_superuser(user, agent)):
            raise HttpError(403, "only whoever asked, an owner of the agent's workspace or a superuser can "
                                 "withdraw this request")
        with transaction.atomic():
            if row.approval_id:
                try:
                    services.supersede(row.approval_id, user, why=f"The AWS role request was withdrawn by "
                                                                 f"{_email(user) or 'its agent'}.")
                except services.ApprovalError as exc:
                    raise HttpError(exc.status, str(exc)) from exc
            row.status = AgentAwsRoleRequest.CANCELLED
            row.save(update_fields=["status"])
        return _out(row)
    if row.status != AgentAwsRoleRequest.APPROVED:
        raise HttpError(409, f"this AWS role request is already {row.status}")
    if not (row.approved_by_id == user.pk or _owner_or_superuser(user, agent)):
        raise HttpError(403, "only the person who approved this grant, an owner of the agent's workspace "
                             "or a superuser can revoke it")
    now = timezone.now()
    row.status = AgentAwsRoleRequest.REVOKED
    row.revoked_by = user
    row.revoked_at = now
    row.save(update_fields=["status", "revoked_by", "revoked_at"])
    return _out(row)
