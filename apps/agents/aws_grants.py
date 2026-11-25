"""Which AWS roles need a person's approval, and the grants that hold one (`AgentAwsRoleRequest`).

The rules are `apps/agents/api_aws.py`'s module docstring. This module reads settings and the
agent's own rows only; filing and deciding the approval is `apps/approvals` (kind `agent.aws_role`,
routes `apps/approvals/api_aws_roles.py`).
"""
from __future__ import annotations

from django.conf import settings
from django.utils import timezone

from .models import AgentAwsRoleRequest

#: The approval's statuses, as the request's own once the approval closed without a yes.
_CLOSED_AS = {"declined": AgentAwsRoleRequest.DECLINED, "cancelled": AgentAwsRoleRequest.CANCELLED,
              "expired": AgentAwsRoleRequest.EXPIRED, "failed": AgentAwsRoleRequest.CANCELLED}


def roles() -> dict[str, str]:
    """Every role canopy-web vends: name -> ARN."""
    return dict(getattr(settings, "CANOPY_AWS_ROLES", {}) or {})


def standing(role: str) -> list[str] | None:
    """The agents that get `role` with no approval, or None when the role is not fenced."""
    only = (getattr(settings, "CANOPY_AWS_ROLE_AGENTS", {}) or {}).get(role)
    return None if only is None else list(only)


def is_fenced(role: str) -> bool:
    return standing(role) is not None


def summary(role: str) -> str:
    """What the role can do, in one line (`settings.CANOPY_AWS_ROLE_SUMMARIES`)."""
    return str((getattr(settings, "CANOPY_AWS_ROLE_SUMMARIES", {}) or {}).get(role) or "").strip()


def settle(row: AgentAwsRoleRequest, now=None) -> AgentAwsRoleRequest:
    """Bring `row.status` up to date: a pending request whose approval closed without a yes takes
    the approval's outcome, and an approved grant past its end is expired."""
    now = now or timezone.now()
    new = row.status
    if row.status == row.PENDING and row.approval_id is not None:
        new = _CLOSED_AS.get(row.approval.status, row.status)
    elif row.status == row.APPROVED and row.expires_at is not None and row.expires_at <= now:
        new = row.EXPIRED
    if new != row.status:
        row.status = new
        row.save(update_fields=["status"])
    return row


def live_grant(agent, role: str, now=None) -> AgentAwsRoleRequest | None:
    """The agent's approved, unrevoked grant for `role` that ends last, if one is live now."""
    now = now or timezone.now()
    return (AgentAwsRoleRequest.objects.filter(agent=agent, role=role, status=AgentAwsRoleRequest.APPROVED,
                                               revoked_at__isnull=True, expires_at__gt=now)
            .order_by("-expires_at").first())
