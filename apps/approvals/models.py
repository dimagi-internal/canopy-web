"""Approval requests: a person's yes or no on ONE specific, typed action.

A request names a `kind` from a fixed registry (`kinds/`), carries that kind's typed `params`,
and holds a SNAPSHOT of what approving will do — rendered by the server from those same params
when it was filed, so the preview an approver reads is exactly what will run, never the
requester's prose. Approving runs the kind's hard-coded handler as the approver (no model in
the loop); declining runs nothing. The requester's own words (`reason`, `evidence`) are kept
apart and shown as theirs.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ApprovalRequest(models.Model):
    PENDING, DONE, FAILED, DECLINED, CANCELLED, EXPIRED = (
        "pending", "done", "failed", "declined", "cancelled", "expired")
    STATUSES = (PENDING, DONE, FAILED, DECLINED, CANCELLED, EXPIRED)
    #: Decided: nothing more can happen to it.
    CLOSED = (DONE, FAILED, DECLINED, CANCELLED, EXPIRED)

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="approval_requests")
    kind = models.CharField(max_length=64)
    params = models.JSONField(default=dict)

    # What approving will do, as the server rendered it from `params` when this was filed.
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True, default="")
    plan = models.JSONField(default=list)
    if_declined = models.CharField(max_length=300, blank=True, default="")
    #: Where it shows, like an alert: `workspace:<slug>`, `agent:<slug>`, `runner:<pk>`.
    scopes = models.JSONField(default=list)

    # The requester's own words, shown as theirs and never as the preview.
    reason = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=list)      # [{quote, who, source_url}]
    source_url = models.URLField(max_length=500, blank=True, default="")
    requested_by_agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name="approval_requests")
    requested_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="+")
    #: Send it to this person only (they must still be able to approve it).
    for_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")

    status = models.CharField(max_length=12, default=PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    #: The door: web | slack | email | push | alert | api.
    via = models.CharField(max_length=16, blank=True, default="")
    comment = models.TextField(blank=True, default="")
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    expires_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["workspace", "status"])]

    @property
    def key(self) -> str:
        return f"APR-{self.pk}"

    @property
    def is_pending(self) -> bool:
        return self.status == self.PENDING

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.key} {self.kind} {self.status}"


class ApprovalEvent(models.Model):
    """The request's audit trail: one row per thing that happened to it."""

    CREATED, DELIVERED, APPROVED, DECLINED, EXECUTED, FAILED, CANCELLED, EXPIRED = (
        "created", "delivered", "approved", "declined", "executed", "failed", "cancelled", "expired")

    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=16)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    payload = models.JSONField(default=dict, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "id"]


class ApprovalDelivery(models.Model):
    """One copy of a request sent to one person over one channel."""

    PUSH, SLACK, EMAIL = "push", "slack", "email"
    CHANNELS = (PUSH, SLACK, EMAIL)

    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name="deliveries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    channel = models.CharField(max_length=16)
    #: What the channel needs to find its copy again (a Slack DM's channel + ts).
    ref = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    #: When the copy was brought up to date with the decision.
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "user", "channel"],
                                               name="approval_request_delivery_once")]
