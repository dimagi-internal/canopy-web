"""Personal Access Tokens (PATs).

A PAT is a long-lived bearer token bound to a single user. The raw token
is shown once at creation; only the sha256 hash is stored. Revocable
per-token; auditable via `last_used_at`.

Replaces the previous shared-secret flow (`/api/auth/e2e-login/` +
`WORKBENCH_WRITE_TOKEN` allowlist) with per-user, per-purpose tokens.
"""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models

from apps.workspaces.models import WorkspaceMembership


class PersonalToken(models.Model):
    """Bearer token issued to a Django user.

    Authentication: `BearerTokenAuthMiddleware` reads the
    `Authorization: Bearer <raw>` header, sha256-hashes the raw value,
    looks up an unrevoked match, and stamps `request.user = token.user`.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_tokens",
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "personal_tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self):
        return f"Token {self.label!r} for {self.user_id}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @classmethod
    def create_for_user(cls, *, user, label: str) -> tuple[str, PersonalToken]:
        """Mint a token. The raw value is returned ONCE — it's never stored.

        The caller is responsible for delivering the raw value to the
        token owner (UI display, env-var dump, etc.).
        """
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = cls.objects.create(user=user, token_hash=token_hash, label=label)
        return raw, token

    @classmethod
    def lookup(cls, raw: str) -> PersonalToken | None:
        """Find an unrevoked token by its raw value. Returns None on miss."""
        if not raw:
            return None
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        return (
            cls.objects.select_related("user")
            .filter(token_hash=token_hash, revoked_at__isnull=True)
            .first()
        )


class AppCredential(models.Model):
    """A registered embedding application (e.g. ace-web). Its ONLY power is the
    token-exchange endpoint: it can mint short-lived DelegatedTokens for humans
    in its allowlisted email domains. It is NOT a user token — BearerTokenAuth
    never resolves it, so it cannot call normal APIs.

    `provision_workspace` / `provision_role` grant this credential's exchange
    calls the additional power to add a JIT-created (or existing) user to ONE
    tenant workspace, at `provision_role`, the first time they exchange. Null
    `provision_workspace` = no provisioning power (the historical behavior).
    The workspace is fixed on this server-side row — it is never client
    input, so an app can only ever provision into the tenant it was granted.
    `provision_role` may never be `owner`: an app must never be able to mint
    an administrator of a tenant (owners can invite/remove members and
    change roles). Enforced both here (`create_credential`) and by a DB-level
    CheckConstraint, so a shell caller bypassing `create_credential` (e.g.
    `AppCredential.objects.create(...)`) is blocked too. See
    docs/superpowers/plans/2026-07-26-tenant-scoped-provisioning.md.
    """

    PROVISION_ROLE_CHOICES = [
        (WorkspaceMembership.VIEWER, "Viewer"),
        (WorkspaceMembership.EDITOR, "Editor"),
    ]

    name = models.CharField(max_length=100, unique=True)
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    allowed_delegation_domains = models.JSONField(default=list)
    provision_workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Tenant this credential may provision JIT/existing users into. "
        "Null = no provisioning power.",
    )
    provision_role = models.CharField(
        max_length=16,
        choices=PROVISION_ROLE_CHOICES,
        default=WorkspaceMembership.EDITOR,
        help_text="Role granted on first provisioning. Never 'owner'.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "app_credentials"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(provision_role=WorkspaceMembership.OWNER),
                name="app_credential_provision_role_not_owner",
            ),
        ]

    @classmethod
    def create_credential(cls, *, name, domains, created_by,
                          provision_workspace=None, provision_role=WorkspaceMembership.EDITOR):
        if provision_role == WorkspaceMembership.OWNER:
            raise ValueError(
                "AppCredential.provision_role may not be 'owner' — an app "
                "credential must never mint an administrator of a workspace"
            )
        raw = secrets.token_urlsafe(32)
        cred = cls.objects.create(
            name=name,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            allowed_delegation_domains=list(domains),
            created_by=created_by,
            provision_workspace=provision_workspace,
            provision_role=provision_role,
        )
        return raw, cred

    @classmethod
    def lookup(cls, raw):
        if not raw:
            return None
        return cls.objects.filter(
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            revoked_at__isnull=True,
        ).first()


class DelegatedToken(models.Model):
    """Short-lived bearer minted by token exchange: <app> acting as <user>.
    DB-backed (not JWT) so it is revocable and the table is the audit trail."""

    app = models.ForeignKey(AppCredential, on_delete=models.CASCADE, related_name="delegated_tokens")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="delegated_tokens")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "delegated_tokens"
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, *, app, user, ttl_seconds):
        from django.utils import timezone
        raw = secrets.token_urlsafe(32)
        token = cls.objects.create(
            app=app, user=user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
        )
        return raw, token

    @classmethod
    def lookup(cls, raw):
        from django.utils import timezone
        if not raw:
            return None
        return (
            cls.objects.select_related("user")
            .filter(token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    expires_at__gt=timezone.now())
            .first()
        )
