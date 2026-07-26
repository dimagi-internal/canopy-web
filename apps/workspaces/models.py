"""Workspace tenancy models — the unit of multi-tenancy.

Ported from ace-web `apps/workspaces`, made domain-agnostic: the ace-specific
`drive_root_folder_id` is dropped (a generic workspace owns no Drive folder). A
Workspace owns members (roles) and pending email invites; agents + runs are
scoped to exactly one workspace (a later increment adds that FK).

This is the tenancy concept — distinct from the retired co-authoring app that
used to be `apps/workspace` (singular).

FRAMEWORK tier: may FK to the auth User + framework models; must not import any
product app. See ARCHITECTURE.md.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def generate_invite_token() -> str:
    """A 48-char URL-safe random invite token."""
    return secrets.token_urlsafe(36)[:48]


class Workspace(models.Model):
    slug = models.CharField(primary_key=True, max_length=64)
    display_name = models.CharField(max_length=200)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workspaces_created",
    )
    settings = models.JSONField(default=dict, blank=True)
    auto_join_domains = models.JSONField(
        default=list,
        blank=True,
        help_text="Email domains (lowercased, no leading '@') whose users are "
        "auto-added as editor on first login.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.slug})"


class WorkspaceMembership(models.Model):
    OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
    ROLE_CHOICES = [(OWNER, "Owner"), (EDITOR, "Editor"), (VIEWER, "Viewer")]
    # Total order used to decide "higher" vs "lower" role — the single place
    # role comparisons live (see `accept_invite`'s upgrade-only semantic and
    # `services.set_member_role`). Do not scatter role comparisons elsewhere.
    ROLE_RANK = {VIEWER: 0, EDITOR: 1, OWNER: 2}

    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="uniq_ws_member"),
        ]
        indexes = [models.Index(fields=["user", "workspace"])]

    def __str__(self) -> str:
        return f"{self.user.email} = {self.role} on {self.workspace.slug}"


class WorkspaceInvite(models.Model):
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="invites"
    )
    email = models.CharField(max_length=200)
    role = models.CharField(
        max_length=16,
        choices=WorkspaceMembership.ROLE_CHOICES,
        default=WorkspaceMembership.EDITOR,
    )
    token = models.CharField(max_length=64, unique=True, default=generate_invite_token)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="invites_sent",
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["email", "-created_at"]),
            models.Index(fields=["workspace", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "email"],
                condition=models.Q(accepted_at__isnull=True, revoked_at__isnull=True),
                name="uniq_ws_invite_live_per_email",
            ),
        ]

    def __str__(self) -> str:
        return f"Invite {self.email} to {self.workspace.slug} as {self.role}"

    def is_pending(self) -> bool:
        if self.accepted_at is not None or self.revoked_at is not None:
            return False
        return self.expires_at > timezone.now()
