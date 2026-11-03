"""OAuth connections to third-party providers (Salesforce, Google), held sealed.

A `ConnectionApp` is one workspace's OAuth client for one provider: the client
id, the client secret and the provider's options, configured by a workspace
admin. Every `Connection` was minted by one app and is refreshed and revoked
with that app's client; there is no site-wide client.

A `Connection` is one account connected so work can be done AS an owner: a
person (their CLI, their sessions) or an agent (its turns). Exactly one owner,
never both, and there is no fallback between them.

canopy-web holds no token it can use. The refresh token is sealed to the owner
(and the machines the owner allows) in `Connection.sealed`; the access token a
machine last minted is sealed beside it. The app's client secret is the only
Fernet ciphertext here (`apps.common.encryption`). What differs between
providers lives in `providers/`, never here: `provider` is just the registry key.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.encryption import decrypt_secret, encrypt_secret

from . import wire


class ConnectionApp(models.Model):
    """One workspace's OAuth client for one provider.

    Removed only through `services.remove_app`, which disconnects and records every
    connection made with it first. `workspace` is PROTECT so deleting a workspace
    cannot bypass that: the workspace delete answers 409 until its admins have
    removed its apps (`workspace_delete_hint` is what that 409 tells them)."""

    # Read by the workspace delete route (core, which cannot import this app) to
    # say what to do about the rows a PROTECT left in its way.
    workspace_delete_hint = "Remove its connection apps in Settings → Connections first."

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="connection_apps",
    )
    provider = models.CharField(max_length=40)
    client_id = models.CharField(max_length=255)
    # Never returned by any route, never logged, never in an event: `client_secret`.
    client_secret_enc = models.TextField(blank=True, default="")
    # The provider's own settings, as its `clean_options` returned them.
    options = models.JSONField(default=dict, blank=True)
    # Extra scopes chosen from the provider's `scope_choices`, space-separated, on
    # top of the ones the provider always asks for.
    scopes = models.CharField(max_length=1000, blank=True, default="")
    configured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    # Set when an admin removes the app: hidden from then on, and deleted once
    # every connection made with it has been revoked.
    removed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "provider"], name="connectionapp_unique_workspace_provider"),
        ]

    def __str__(self) -> str:
        return f"{self.workspace_id}:{self.provider}"

    @property
    def client_secret(self) -> str:
        """The decrypted secret; "" when none is stored or it cannot be read."""
        if not self.client_secret_enc:
            return ""
        try:
            return decrypt_secret(self.client_secret_enc)
        except Exception:  # noqa: BLE001 - an unreadable secret is a missing one
            return ""

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_enc = encrypt_secret(value) if value else ""

    @property
    def extra_scopes(self) -> tuple[str, ...]:
        return tuple(self.scopes.split())


class Connection(models.Model):
    """One account an owner connected: the sealed refresh token and the metadata
    around it. canopy-web can open `sealed` and `access_sealed` only when they name its own KMS key
    (`server_use`, `server_tokens`).

    `sealed` is a `zks1` envelope (name `refresh`) bound to `binding`; at connect
    it is sealed to the owner's member key only (`state = unshared`), and the
    owner's browser re-seals it to the machines its signed grant statement
    allows (`active`). `access_sealed` (name `access`) is the current access
    token, written by whoever last refreshed. `sealed_sig` /
    `access_sig` are each envelope's writer MACs (null only on canopy-web's
    first seal). `sealed_digest` is SHA-256 of `sealed`, for compare-and-swap
    writes. The `lease_*` columns are the one refresh lease. The wire format is
    `docs/vault/zk-v1/connections_v1_reference.py`."""

    UNSHARED = "unshared"
    ACTIVE = "active"
    REVOKING = "revoking"
    STATES = [(UNSHARED, "Not shared yet"), (ACTIVE, "Active"), (REVOKING, "Revoking")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="connections",
    )
    agent = models.ForeignKey(
        "agents.Agent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="connections",
    )
    # The workspace app that minted this grant; machines refresh and revoke it with
    # that app's client. CASCADE for the app's own removal, which happens only
    # once its connections are gone (an app's workspace FK is PROTECT).
    app = models.ForeignKey(ConnectionApp, on_delete=models.CASCADE, related_name="connections")
    provider = models.CharField(max_length=40)
    # The provider's stable id for the account (Salesforce: "<org id>/<user id>").
    account_id = models.CharField(max_length=255)
    # Display only — never used for authorization.
    account_label = models.CharField(max_length=255, blank=True, default="")
    # Display only: a machine takes the API base from the sealed plaintext, checked
    # against the provider's host rules, never from here.
    instance_url = models.URLField(blank=True, default="")
    # What the provider GRANTED (its `scope`), space-separated; what was asked for
    # when it did not say.
    scopes = models.CharField(max_length=500, blank=True, default="")
    state = models.CharField(max_length=10, choices=STATES, default=UNSHARED)

    sealed = models.TextField(blank=True, default="")
    sealed_sig = models.JSONField(null=True, blank=True)
    sealed_digest = models.CharField(max_length=64, blank=True, default="")
    access_sealed = models.TextField(blank=True, default="")
    access_sig = models.JSONField(null=True, blank=True)
    access_expires_at = models.DateTimeField(null=True, blank=True)
    # RFC 7638 thumbprint of the owner's member public key, sealed inside `sealed`
    # too; only its holder signs the grant statement.
    owner_key = models.CharField(max_length=64, blank=True, default="")
    # Whose member key `owner_key` is: the person, or the admin who connected an
    # agent's. SET_NULL: the row outlives them (another admin disconnects it, then connects).
    signer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    # That key and that person as they were at connect, kept so machines can still judge
    # and revoke this row after the signer replaced their key or was deleted. A convenience
    # and never a trust: a reader checks this key's thumbprint against the `owner_key`
    # sealed in the envelope, so a server that changes these only gets itself refused.
    owner_public_jwk = models.JSONField(default=dict, blank=True)
    owner_user_id = models.CharField(max_length=32, blank=True, default="")
    # The owner's endorsements of machines as they stood when a share or a keeper's re-seal
    # landed, each verified under `owner_public_jwk`. Re-endorsing under a new key overwrites
    # the live row, and every machine would then refuse a writer only the old signature vouches
    # for, including for a revoke. Readers verify these as any other: never on the server's word.
    owner_endorsements = models.JSONField(default=list, blank=True)
    # The switch: canopy-web's own key (`server_key`) may open this connection. Only the owner's
    # choice moves it: a reconnect never resets it to the provider's default. A machine trusts
    # the statement's `canopy_web`, never this (`recipients.server_recipient`).
    server_use = models.BooleanField(default=False)
    # The owner's signed grant statement (the machines it may reach); version 0 = none yet.
    statement = models.JSONField(null=True, blank=True)
    statement_sig = models.CharField(max_length=128, blank=True, default="")
    statement_version = models.PositiveIntegerField(default=0)

    # The refresh lease: one holder (a device's ECDH thumbprint) at a time.
    lease_id = models.UUIDField(null=True, blank=True)
    lease_holder = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_written = models.BooleanField(default=False)

    # Set when a machine's refresh is rejected (`invalid_grant`); cleared by a reconnect.
    refresh_failed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoking_since = models.DateTimeField(null=True, blank=True)
    # Whether the holder that confirms the revoke must also revoke at the provider
    # (not when another connection of that account through that app remains).
    revoke_at_provider = models.BooleanField(default=True)
    # Who completed the sign-in. SET_NULL: an agent's connection outlives the
    # admin who connected it.
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(user__isnull=False, agent__isnull=True)
                           | Q(user__isnull=True, agent__isnull=False)),
                name="connection_exactly_one_owner",
            ),
            # Per app: the same account connected through two workspaces' apps is two grants.
            models.UniqueConstraint(
                fields=["user", "app", "account_id"], name="connection_unique_user_app_account",
            ),
            models.UniqueConstraint(
                fields=["agent", "app", "account_id"], name="connection_unique_agent_app_account",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.owner_label} -> {self.provider}:{self.account_label or self.account_id}"

    @property
    def owner_label(self) -> str:
        if self.agent_id is not None:
            return f"agent:{self.agent.slug}"
        if self.user_id is not None:
            return self.user.email or self.user.get_username()
        return ""

    @property
    def needs_reconnect(self) -> bool:
        """True once a machine's refresh was rejected: the owner must connect again."""
        return self.refresh_failed_at is not None

    @property
    def binding(self) -> str:
        """`connection:<uuid>`, the binding id both envelopes are sealed to."""
        return wire.binding_id(self.id)


class ConnectionGrant(models.Model):
    """A connection lent to an agent other than its owner: a person's to an agent
    they administer, or one agent's to another. Who may lend is checked where a
    grant statement is accepted; this row only records it."""

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="grants")
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="connection_grants",
    )
    # SET_NULL for the same reason as `Connection.connected_by`: the grant stands
    # until an admin revokes it, whoever made it.
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "agent"], name="connectiongrant_unique"),
        ]
