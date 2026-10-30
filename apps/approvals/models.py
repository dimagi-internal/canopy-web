"""Approval cards — the delivery and decision record around a board task.

The card's CONTENT is the task itself (`agents.AgentTask`: its ask, evidence quotes, plan,
`on_approve` turns). These two tables only remember what canopy did with it: where it was
sent, so a decision can update every copy, and which door a decision came through.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ApprovalDelivery(models.Model):
    """One copy of a card sent to one person over one channel."""

    PUSH, SLACK, EMAIL = "push", "slack", "email"
    CHANNELS = (PUSH, SLACK, EMAIL)

    task = models.ForeignKey("agents.AgentTask", on_delete=models.CASCADE, related_name="approval_deliveries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    channel = models.CharField(max_length=16)
    #: What the channel needs to find its copy again (a Slack DM's channel + ts).
    ref = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    #: When the copy was brought up to date with the decision.
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["task", "user", "channel"],
                                               name="approval_delivery_once")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.task_id}->{self.user_id}:{self.channel}"


class ApprovalDecision(models.Model):
    """Who decided a card, through which door, and what they said."""

    APPROVE, DECLINE = "approve", "decline"
    task = models.ForeignKey("agents.AgentTask", on_delete=models.CASCADE, related_name="approval_decisions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    decision = models.CharField(max_length=10)
    #: web | slack | email | push | api — the door, as the card names it.
    via = models.CharField(max_length=16, default="web")
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
