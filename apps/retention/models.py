"""How long canopy-web keeps the CONTENT of AI conversations and turns.

A `RetentionRule` says "content matching these filters is dropped after N
days". `RetentionSweep` records each run: counts only, never content. The
resolution (which rule governs an item) is `policy.py`; the purge is
`services.py`. Spec: docs/superpowers/specs/2026-10-05-content-retention-design.md.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

MIN_KEEP_DAYS = 1


class RetentionRule(models.Model):
    """Every field but `keep_days` is a filter; blank/null means "any"."""

    CHAT, TURN, SHARED = "chat", "turn", "shared"
    KIND_CHOICES = [
        (CHAT, "Chat: a conversation and every turn on it"),
        (TURN, "Turn: agent/project work that is not a chat"),
        (SHARED, "Shared transcript: an uploaded /canopy:share-session"),
    ]

    MEMBER, CONTACT, AGENT, SYSTEM = "member", "contact", "agent", "system"
    PRINCIPAL_CHOICES = [
        (MEMBER, "Member: a canopy user"),
        (CONTACT, "Contact: an outside person with no account"),
        (AGENT, "Agent: another agent dispatched it"),
        (SYSTEM, "System: a schedule, a drill, or nobody established"),
    ]

    # The Turn.origin vocabulary, plus `emdash` for a chat discovered on a
    # runner rather than started in an app. Not imported from harness so the
    # choices stay readable in a migration; `tests/test_retention.py` pins them
    # to Turn.ORIGIN_CHOICES.
    EMDASH = "emdash"
    SOURCE_CHOICES = [
        ("api", "API"), ("ace_web", "ace-web"), ("canopy_web_chat", "canopy-web chat"),
        ("canopy_scheduler", "canopy scheduler"), ("email", "Email"), ("slack", "Slack"),
        (EMDASH, "emdash (runner-discovered chat)"),
    ]

    #: Null = deployment-wide. A rule on a workspace also governs its children
    #: unless they have a matching rule of their own (nearest workspace wins).
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
        related_name="retention_rules",
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, blank=True, default="")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, blank=True, default="")
    principal = models.CharField(max_length=8, choices=PRINCIPAL_CHOICES, blank=True, default="")
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, null=True, blank=True,
        related_name="retention_rules",
    )
    #: Null = keep forever: an explicit exemption that beats a broader rule.
    keep_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Drop matching content this many days after it was written. Blank = keep forever.",
    )
    note = models.TextField(blank=True, default="", help_text="Why this rule exists.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["workspace_id", "kind", "source", "principal"]

    def __str__(self) -> str:  # pragma: no cover
        keep = "forever" if self.keep_days is None else f"{self.keep_days}d"
        return f"retention:{self.scope_label()}:{self.filter_label()}:{keep}"

    @property
    def specificity(self) -> int:
        """How many filters are set. More specific rules win within a scope."""
        return sum(bool(v) for v in (self.kind, self.source, self.principal, self.agent_id))

    def scope_label(self) -> str:
        return self.workspace_id or "*"

    def filter_label(self) -> str:
        parts = [
            f"kind={self.kind}" if self.kind else "",
            f"source={self.source}" if self.source else "",
            f"principal={self.principal}" if self.principal else "",
            f"agent={self.agent.slug}" if self.agent_id else "",
        ]
        return " ".join(p for p in parts if p) or "everything"

    def clean(self) -> None:
        if self.keep_days is not None and self.keep_days < MIN_KEEP_DAYS:
            raise ValidationError({"keep_days": f"at least {MIN_KEEP_DAYS} day"})
        if self.agent_id and self.workspace_id and self.agent.workspace_id != self.workspace_id:
            raise ValidationError({"agent": "that agent lives in another workspace"})
        if self.kind == self.SHARED and (self.workspace_id or self.agent_id or self.source):
            # A shared transcript has no workspace, agent or source to match on,
            # so such a rule could never apply, and that silence would mislead.
            raise ValidationError(
                {"kind": "a shared-transcript rule can only be deployment-wide and match on principal"}
            )
        # Two rules with the same scope and filters cannot be ranked. Checked
        # here rather than as a constraint because NULLs never collide in a
        # UNIQUE index.
        dup = RetentionRule.objects.filter(
            workspace_id=self.workspace_id, kind=self.kind, source=self.source,
            principal=self.principal, agent_id=self.agent_id,
        ).exclude(pk=self.pk)
        if dup.exists():
            raise ValidationError("a rule with exactly these filters already exists; edit that one")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class RetentionSweep(models.Model):
    """One run of the purge, dry or real. Counts only, so it never becomes a
    copy of what it deleted."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    applied = models.BooleanField(default=False, help_text="False = dry run; nothing was deleted.")
    trigger = models.CharField(max_length=16, default="command")  # command | heartbeat
    counts = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"sweep:{self.started_at:%Y-%m-%d %H:%M}:{'applied' if self.applied else 'dry'}"
