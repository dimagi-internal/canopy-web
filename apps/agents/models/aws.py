"""Agent models: an agent's request for a fenced AWS role, and the grant a person's yes makes it.

The rules are `apps/agents/api_aws.py`'s module docstring; this row is the record of one ask.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from .agent import Agent


class AgentAwsRoleRequest(models.Model):
    """An agent asking for a fenced AWS role (`settings.CANOPY_AWS_ROLE_AGENTS`) for a while.

    It is filed with an approval request of kind `agent.aws_role` (`approval`). A workspace owner
    of the agent's workspace, or a canopy superuser, approves it on the approval's page with a
    passkey; approving makes it a grant that lasts `minutes` from then (`expires_at`). Until
    then, or once it is declined, withdrawn, expired or revoked, `aws-credentials` refuses the
    role to the agent."""

    PENDING, APPROVED, DECLINED, CANCELLED, EXPIRED, REVOKED = (
        "pending", "approved", "declined", "cancelled", "expired", "revoked")
    STATUSES = (PENDING, APPROVED, DECLINED, CANCELLED, EXPIRED, REVOKED)

    DEFAULT_MINUTES = 60
    MIN_MINUTES = 15  # STS's shortest session: a grant that ends sooner could not be vended in full
    MAX_MINUTES = 8 * 60

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="aws_role_requests")
    role = models.CharField(max_length=64)
    reason = models.TextField()
    minutes = models.PositiveIntegerField(default=DEFAULT_MINUTES)
    status = models.CharField(max_length=12, default=PENDING, db_index=True)
    #: The person who asked for the agent; null when the agent's own login asked.
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    approval = models.OneToOneField("approvals.ApprovalRequest", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="aws_role_request")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    #: When the grant ends: `approved_at + minutes`. Null until approved.
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["agent", "role", "status"])]

    def is_live(self, now=None) -> bool:
        """An approved grant that has not ended and was not revoked."""
        now = now or timezone.now()
        return (self.status == self.APPROVED and self.revoked_at is None
                and self.expires_at is not None and self.expires_at > now)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.role} ({self.status})"
