"""Kind: let an agent take a fenced AWS role for a while.

Filed by `POST /api/agents/{slug}/aws-role-requests` (`apps/approvals/api_aws_roles.py`), never by
hand: its one param is the server-made `AgentAwsRoleRequest`, which holds the agent, the role, the
duration and the reason. These are AWS account rights, so the bar is a workspace owner of the
agent's workspace or a canopy superuser, with a passkey; never an agent, and never the agent's own
admins as such. Approving makes the request a grant that lasts its minutes from now; `aws-credentials`
then vends the role to the agent until the grant ends or is revoked (`apps/agents/api_aws.py`).
"""
from __future__ import annotations

import datetime as dt

from django.utils import timezone

from apps.agents import aws_grants, aws_sealed
from apps.agents.models import Agent, AgentAwsRoleRequest
from apps.common.schemas import StrictModel
from apps.workspaces import permissions as perms

from . import Described, ExecError, Kind, KindError, register

SLUG = "agent.aws_role"


class Params(StrictModel):
    request: int


def duration_words(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    parts = ([f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []) + (
        [f"{mins} minute{'s' if mins != 1 else ''}"] if mins else [])
    return " ".join(parts) or "0 minutes"


def _row(pk: int) -> AgentAwsRoleRequest:
    row = AgentAwsRoleRequest.objects.select_related("agent", "agent__user").filter(pk=pk).first()
    if row is None:
        raise KindError(f"there is no AWS role request {pk}")
    return row


def is_agent_login(user) -> bool:
    return Agent.objects.filter(user_id=getattr(user, "pk", None)).exists()


@register
class AgentAwsRole(Kind):
    slug = SLUG
    label = "Let an agent take an AWS role"
    params_model = Params
    needs = ("an owner or superuser of the agent's workspace, or a canopy superuser who is a member of it, "
             "with a passkey confirmation")
    does = ("Lets the agent take the named AWS role from canopy-web for the minutes asked, from the moment "
            "it is approved. Each set of keys lasts at most an hour and never past the grant, and AWS "
            "names the agent on every call. The approver or a workspace owner can revoke it.")
    step_up = True

    def describe(self, p: Params) -> Described:
        row = _row(p.request)
        agent = row.agent
        name = agent.name or agent.slug
        arn = aws_grants.roles().get(row.role, "")
        what = aws_grants.summary(row.role) or "(canopy has no summary for this role)"
        lasts = duration_words(row.minutes)
        sealed = row.delivery == AgentAwsRoleRequest.SEALED
        how = ("Your browser makes the keys with the role's sealed key, after your passkey touch, and seals them "
               f"to {name}'s machines: canopy-web never holds a usable key"
               if sealed else "canopy-web makes the keys with its own AWS role")
        return Described(
            workspace=agent.workspace_id,
            title=f"Let {name} take the AWS role {row.role} for {lasts}",
            summary=f"{name} could use the AWS role {row.role} for {lasts} from your approval. {what}",
            plan=[f"The agent {name} ({agent.workspace_id}) may take the AWS role {row.role}"
                  + (f" ({arn})" if arn else ""),
                  f"What {row.role} can do: {what}",
                  (f"For {lasts} from your approval; your browser makes one set of keys that never outlasts it"
                   if sealed else
                   f"For {lasts} from your approval; each set of keys lasts at most an hour and never past that"),
                  how,
                  f"AWS records every call as canopy-agent-{agent.slug}",
                  "You or an owner of the workspace can revoke it at any time; keys already handed out "
                  "last until their own expiry",
                  "Approving needs your passkey"],
            scopes=[f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"],
            about_email=(getattr(agent.user, "email", "") or "").lower())

    def may_approve(self, user, p: Params, d: Described) -> bool:
        if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", True):
            return False
        if is_agent_login(user):  # never an agent: not the one asking, not another
            return False
        if d.about_email and (user.email or "").lower() == d.about_email:
            return False
        if not (bool(user.is_superuser) or perms.can(user, d.workspace, perms.OWN)):
            return False
        row = _row(p.request)
        # A sealed grant is useless unless its approver's browser can make the session.
        return row.delivery != AgentAwsRoleRequest.SEALED or aws_sealed.may_mint(user, row.role)

    def client_step(self, p: Params) -> dict | None:
        row = _row(p.request)
        if row.delivery != AgentAwsRoleRequest.SEALED:
            return None
        return {"name": "aws.sealed_session", "agent": row.agent.slug, "request": row.pk}

    def execute(self, user, p: Params, request) -> dict:
        row = AgentAwsRoleRequest.objects.select_for_update().filter(pk=p.request).first()
        if row is None or row.approval_id != request.pk:
            raise ExecError("This approval is not the one filed for that AWS role request.")
        if row.status != AgentAwsRoleRequest.PENDING:
            raise ExecError(f"That AWS role request is {row.status}, not waiting for approval.")
        if row.role not in aws_grants.roles() or not aws_grants.is_fenced(row.role):
            raise ExecError(f"canopy no longer fences the AWS role {row.role}; nothing to approve.")
        now = timezone.now()
        row.status = AgentAwsRoleRequest.APPROVED
        row.approved_by = user
        row.approved_at = now
        row.expires_at = now + dt.timedelta(minutes=row.minutes)
        row.save(update_fields=["status", "approved_by", "approved_at", "expires_at"])
        return {"request": row.pk, "agent": row.agent.slug, "role": row.role,
                "expires_at": row.expires_at.isoformat()}
