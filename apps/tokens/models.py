"""Personal Access Tokens (PATs).

A PAT is a bearer token bound to a single user. The raw token is shown once at
creation; only the sha256 hash is stored. Revocable per-token; auditable via
`last_used_at`. Tokens expire after `settings.PAT_DEFAULT_TTL_DAYS` (180) unless
minted with an explicit `ttl_days` — where 0 means "never expires", matching the
GitHub PAT model and the labs `mcp_create_token --ttl-days 0` convention.

Replaces the previous shared-secret flow (`/api/auth/e2e-login/` +
`WORKBENCH_WRITE_TOKEN` allowlist) with per-user, per-purpose tokens.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

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
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this token stops authenticating. NULL means it never expires — "
            "which covers both tokens minted before expiry existed (grandfathered "
            "by migration 0005, which deliberately has no data step) and tokens "
            "minted with ttl_days=0. Enforced in lookup(), not by callers."
        ),
    )

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
        """Display-only truth. MUST agree with lookup()'s filter — lookup is the
        security boundary; this is what admin and the API render. A divergence
        between the two would let a surface report healthy while auth rejects it.
        """
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > timezone.now()

    @classmethod
    def create_for_user(
        cls, *, user, label: str, ttl_days: int | None = None
    ) -> tuple[str, PersonalToken]:
        """Mint a token. The raw value is returned ONCE — it's never stored.

        `ttl_days` follows the same rule on every surface (API and CLI):
        None uses settings.PAT_DEFAULT_TTL_DAYS, 0 means never expires, and any
        positive integer is that many days. There is deliberately no upper bound:
        with 0 available, a cap would only mislead a caller asking for a long TTL
        into silently receiving a shorter one.

        The caller is responsible for delivering the raw value to the
        token owner (UI display, env-var dump, etc.).
        """
        if ttl_days is None:
            ttl_days = getattr(settings, "PAT_DEFAULT_TTL_DAYS", 180)
        ttl_days = int(ttl_days)
        if ttl_days < 0:
            raise ValueError("ttl_days cannot be negative (0 means never expires)")
        expires_at = (
            None if ttl_days == 0 else timezone.now() + timedelta(days=ttl_days)
        )
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        token = cls.objects.create(
            user=user, token_hash=token_hash, label=label, expires_at=expires_at
        )
        return raw, token

    @classmethod
    def lookup(cls, raw: str) -> PersonalToken | None:
        """Find a live (unrevoked, unexpired) token by its raw value.

        THE enforcement point for both bearer auth (`BearerTokenAuthMiddleware`)
        and MCP (`CanopyPATVerifier`) — both resolve tokens through here, so
        expiry lands on both surfaces at once and cannot drift between them.
        A NULL expires_at never expires (see the field's help_text).
        """
        if not raw:
            return None
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        return (
            cls.objects.select_related("user")
            .filter(token_hash=token_hash, revoked_at__isnull=True)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
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
    `provision_role` must be one of `PROVISION_ROLE_CHOICES` (viewer/editor) —
    `owner` is deliberately excluded from the choices, since an app must never
    be able to mint an administrator of a tenant (owners can invite/remove
    members and change roles). This is an ALLOWLIST, not an owner-only
    denylist: an unrecognized value ("Owner", "admin", a typo) is rejected
    the same as `owner` — both here (`create_credential`) and by a DB-level
    CheckConstraint (`provision_role__in=[...]`), so a shell caller bypassing
    `create_credential` (e.g. `AppCredential.objects.create(...)`) is blocked
    too, and nothing but a known-valid role can ever reach
    `WorkspaceMembership.role` (which `ROLE_RANK[...]` would otherwise
    KeyError on downstream, e.g. in `accept_invite`).

    Ordering with domain-wide auto-join: the exchange applies this
    credential's provisioning grant BEFORE `apps.workspaces.services
    .auto_join_workspaces`, so if `provision_workspace` also happens to be
    an auto-join workspace for the user's domain, the explicit
    `provision_role` wins (creates the row first) and the later auto-join
    call is a no-op against it (`ensure_member` is create-only). Without
    this ordering, auto-join running first would silently create the row at
    `editor` regardless of a `viewer` grant — `provision_role` would not be
    a durable ceiling. See docs/archive/plans/
    2026-07-26-tenant-scoped-provisioning.md (design + F3 fix).
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
                condition=models.Q(
                    provision_role__in=[WorkspaceMembership.VIEWER, WorkspaceMembership.EDITOR]
                ),
                name="app_credential_provision_role_valid",
            ),
        ]

    @classmethod
    def create_credential(cls, *, name, domains, created_by,
                          provision_workspace=None, provision_role=WorkspaceMembership.EDITOR):
        valid_roles = dict(cls.PROVISION_ROLE_CHOICES)
        if provision_role not in valid_roles:
            raise ValueError(
                f"AppCredential.provision_role must be one of {sorted(valid_roles)} "
                f"— got {provision_role!r}. An app credential must never mint a "
                "workspace owner or an unrecognized role."
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
