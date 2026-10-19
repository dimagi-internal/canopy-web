"""Workspace email domains: a workspace's own domain, run through Amazon SES.

PRODUCT tier and removable: nothing in a framework app imports this module.
Removing the app drops these tables and nothing else notices. See
docs/superpowers/specs/2026-10-12-workspace-email-domains-design.md.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class MailDomain(models.Model):
    PENDING, VERIFIED, SUSPENDED = "pending", "verified", "suspended"
    STATUS_CHOICES = [(PENDING, "Pending DNS"), (VERIFIED, "Verified"), (SUSPENDED, "Suspended")]

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_domains")
    name = models.CharField(max_length=253, unique=True)
    """Lowercased. Globally unique: an SES identity is account-wide, so one domain
    can belong to one workspace only."""
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    receiving = models.BooleanField(default=False)
    """Whether the domain's MX points at SES right now (live check, cached)."""
    mx_provider = models.CharField(max_length=120, blank=True, default="")
    """Who receives mail for the domain today, named for the cut-over warning."""
    mail_from_subdomain = models.CharField(max_length=63, default="bounce")
    dkim_tokens = models.JSONField(default=list, blank=True)
    suspended_reason = models.TextField(blank=True, default="")
    daily_send_cap = models.PositiveIntegerField(default=200)
    checked_at = models.DateTimeField(null=True, blank=True)
    verification_error = models.TextField(blank=True, default="")
    """Why SES stopped verifying (its DKIM or MAIL FROM status is FAILED), from the last
    SES read; "" while it is verifying or verified. Status stays `pending` meanwhile."""
    verification_restarted_at = models.DateTimeField(null=True, blank=True)
    """When a Re-check last asked SES to start verifying again (rate-limits restarts)."""
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover
        return self.name

    @property
    def routes_live(self) -> bool:
        """Routing is inert until DKIM verifies: that is the proof of DNS control."""
        return self.status in (self.VERIFIED, self.SUSPENDED)


class MailDomainShare(models.Model):
    """A superuser's grant letting another workspace hold addresses on a domain.

    Covers the named workspace AND every workspace below it; a grant on the domain's own
    workspace shares it with that workspace's descendants. The domain's own workspace
    keeps DNS, the domain-wide cap and suspension; a sharing workspace only adds routes
    for its own agents and members, under its own `daily_send_cap`."""
    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="shares")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_domain_shares")
    daily_send_cap = models.PositiveIntegerField(default=200)
    """Recipients per 24 h for each workspace this grant covers (counted per workspace)."""
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["domain__name", "workspace_id"]
        constraints = [
            models.UniqueConstraint(fields=["domain", "workspace"], name="mail_domain_share_once"),
        ]


class MailRoute(models.Model):
    AGENT, FORWARD, DROP = "agent", "forward", "drop"
    KIND_CHOICES = [(AGENT, "Agent mailbox"), (FORWARD, "Forward to a member"), (DROP, "Drop")]
    CATCH_ALL = "*"

    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="routes")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_routes")
    """The workspace that holds this address: the domain's own, or one it is shared with.
    Its threads, messages, agent and forward destination all belong to this workspace."""
    local_part = models.CharField(max_length=64)
    """Lowercased; `*` is the catch-all. Exact match wins over `*`."""
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.CASCADE,
                              related_name="mail_routes")
    forward_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="mail_forwards")
    """A canopy USER, never a typed address — the feature cannot be used as a relay."""
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["domain__name", "local_part"]
        constraints = [
            models.UniqueConstraint(fields=["domain", "local_part"], name="mail_route_unique_local"),
            models.CheckConstraint(
                name="mail_route_target_matches_kind",
                condition=(
                    models.Q(kind="agent", agent__isnull=False, forward_to__isnull=True)
                    | models.Q(kind="forward", agent__isnull=True, forward_to__isnull=False)
                    | models.Q(kind="drop", agent__isnull=True, forward_to__isnull=True)
                ),
            ),
        ]

    @property
    def address(self) -> str:
        return f"{self.local_part}@{self.domain.name}"

    def save(self, *args, **kwargs):
        if self.workspace_id is None and self.domain_id:
            self.workspace_id = self.domain.workspace_id
        super().save(*args, **kwargs)


class MailThread(models.Model):
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="mail_threads")
    route = models.ForeignKey(MailRoute, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="threads")
    """NULL once the address's route is removed: the conversation outlives the route."""
    address = models.CharField(max_length=320, blank=True, default="")
    """The mailbox address this thread belongs to, snapshotted from the route at creation."""
    agent_slug = models.CharField(max_length=100, blank=True, default="", db_index=True)
    """The agent whose mailbox received the thread, snapshotted at creation ("" for a
    non-agent route), so the agent's list keeps the thread after its route is removed."""
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    """The `thread_id` turns and agents carry."""
    subject = models.CharField(max_length=998, blank=True, default="")
    message_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    archived = models.BooleanField(default=False)
    unread = models.BooleanField(default=True)

    class Meta:
        ordering = ["-last_message_at"]

    def save(self, *args, **kwargs):
        if not self.address and self.route_id:
            self.address = self.route.address
        if not self.agent_slug and self.route_id and self.route.agent_id:
            self.agent_slug = self.route.agent.slug
        super().save(*args, **kwargs)


class MailMessage(models.Model):
    IN, OUT = "in", "out"
    SENT, DELIVERED, BOUNCED, COMPLAINED, REJECTED = "sent", "delivered", "bounced", "complained", "rejected"

    thread = models.ForeignKey(MailThread, on_delete=models.CASCADE, related_name="messages")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="+")
    """Denormalised from thread for the per-workspace unique constraint."""
    direction = models.CharField(max_length=3, choices=[(IN, "in"), (OUT, "out")])
    mailbox = models.CharField(max_length=320, default="")
    """The route address this copy belongs to. One email to two agents is two rows —
    dedupe is per mailbox, so the second agent is never told it is a duplicate."""
    message_id = models.CharField(max_length=998)
    in_reply_to = models.CharField(max_length=998, blank=True, default="")
    references = models.TextField(blank=True, default="")
    from_addr = models.CharField(max_length=320, blank=True, default="")
    from_name = models.CharField(max_length=320, blank=True, default="")
    to = models.JSONField(default=list, blank=True)
    cc = models.JSONField(default=list, blank=True)
    reply_to = models.JSONField(default=list, blank=True, db_default=[])
    """The inbound message's Reply-To addresses; a reply goes there, else to from_addr."""
    subject = models.CharField(max_length=998, blank=True, default="")
    text = models.TextField(blank=True, default="")
    html = models.TextField(blank=True, default="")
    raw_key = models.CharField(max_length=512, blank=True, default="")
    verdicts = models.JSONField(default=dict, blank=True)
    """SES receipt verdicts of an inbound message, lowercase name → status
    (spf, dkim, dmarc, spam, virus: PASS | FAIL | GRAY | PROCESSING_FAILED | DISABLED)."""
    auth_results = models.TextField(blank=True, default="")
    ses_message_id = models.CharField(max_length=128, blank=True, default="", db_index=True)
    delivery_status = models.CharField(max_length=12, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "mailbox", "message_id"], name="mail_message_unique_id"),
        ]

    def save(self, *args, **kwargs):
        if self.workspace_id is None and self.thread_id:
            self.workspace_id = self.thread.workspace_id
        if not self.mailbox and self.thread_id:
            self.mailbox = self.thread.address
        super().save(*args, **kwargs)


class MailAttachment(models.Model):
    message = models.ForeignKey(MailMessage, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=255, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    s3_key = models.CharField(max_length=512)


class MailForwardLog(models.Model):
    """One row per forwarded message — no body: canopy keeps no human mail it only passes on."""
    FORWARDED, DROPPED, SENDING = "forwarded", "dropped", "sending"

    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="forwards")
    route = models.ForeignKey(MailRoute, null=True, on_delete=models.SET_NULL, related_name="+")
    message_id = models.CharField(max_length=998)
    recipient = models.CharField(max_length=320)
    original_from = models.CharField(max_length=320, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    destination = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                    related_name="+")
    status = models.CharField(max_length=10)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["domain", "message_id", "recipient"],
                                    name="mail_forward_once"),
        ]


class MailEvent(models.Model):
    """An SES sending event, kept for the reputation window (7 days is all that is read)."""
    domain = models.ForeignKey(MailDomain, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=16)  # Send | Delivery | Bounce | Complaint | Reject
    hard = models.BooleanField(default=False)  # a Permanent bounce
    ses_message_id = models.CharField(max_length=128, blank=True, default="")
    sns_message_id = models.CharField(max_length=128, blank=True, default="")
    """The SNS envelope MessageId: the exact identity of one delivery, so a redelivery
    counts once while several Bounce notifications for one multi-recipient send all count."""
    at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["domain", "kind", "at"])]
        constraints = [
            models.UniqueConstraint(fields=["sns_message_id"], name="mail_event_once",
                                    condition=~models.Q(sns_message_id="")),
        ]
