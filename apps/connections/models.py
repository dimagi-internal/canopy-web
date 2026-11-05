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
from django.utils import timezone

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


class ConnectionQuerySet(models.QuerySet):
    def unfinished(self):
        """An agent's connects still waiting on their owner's passkey. Connecting an agent means
        the agent has it, so until the first signed share nothing lists these as connected: the
        Connected screen finishes one or says it failed, and the sweep removes what is left. A
        reconnect that didn't finish is not one of these: it keeps its statement and shows as
        needing a reconnect. A person's connection is finished at sign-in, sealed to their key."""
        return self.filter(agent__isnull=False, state=Connection.UNSHARED, statement_version=0)


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

    objects = ConnectionQuerySet.as_manager()

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
    # choice moves it: a reconnect never resets it to the provider's default, though one naming
    # limits turns it off (`limited`). A machine trusts
    # the statement's `canopy_web`, never this (`recipients.server_recipient`).
    server_use = models.BooleanField(default=False, db_default=False)
    # The owner's signed grant statement (the machines it may reach); version 0 = none yet.
    statement = models.JSONField(null=True, blank=True)
    statement_sig = models.CharField(max_length=128, blank=True, default="")
    statement_version = models.PositiveIntegerField(default=0)

    # The refresh lease: one holder (a device's ECDH thumbprint) at a time.
    lease_id = models.UUIDField(null=True, blank=True)
    lease_holder = models.CharField(max_length=64, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_written = models.BooleanField(default=False)

    # Set when a refresh is rejected (`invalid_grant`); cleared by a reconnect or a good write-back.
    refresh_failed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoking_since = models.DateTimeField(null=True, blank=True)
    # Whether the holder that confirms the revoke must also revoke at the provider
    # (not when another connection of that account through that app remains).
    revoke_at_provider = models.BooleanField(default=True)
    # A lend with limits, chosen when the owner started connecting and shown on the share
    # screen. `expires_at`: no machine is handed it from then on, and the sweep disconnects it,
    # which revokes it at the provider. Null: until it is disconnected. `owner_turns_only`:
    # staged only on a turn its owner started (`services.started_by_owner`). canopy-web enforces
    # both, so neither is proof against a hacked canopy-web; that one still cannot read a token.
    expires_at = models.DateTimeField(null=True, blank=True)
    owner_turns_only = models.BooleanField(default=False)
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
    def expired(self) -> bool:
        """True once a lend's time has run out, before the sweep has disconnected it."""
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def limited(self) -> bool:
        """Lent with limits, so narrowly that canopy-web never uses it: no switch, no canopy-web box
        on the share screen, and a statement naming canopy-web is refused."""
        return self.expires_at is not None or self.owner_turns_only

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


class ConnectionOperation(models.Model):
    """A lease on writing from one screen, opened by one person (`call_service`).

    canopy-web can't tell a click from a script; the page around the screen decides that before it
    asks for one. What a row does is limit how far a write can go: this person, this screen, this
    revision, until `expires_at`, at most `call_service.OPERATION_MAX_WRITES` writes. It slides:
    each write moves `expires_at` to `call_service.OPERATION_SECONDS` from then, never past
    `hard_limit_at`, so a long run of writes isn't cut off midway and one click still has an end.
    Expired rows are deleted by `keeper.sweep`."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    # `declarations.Surface.kind` and `.full_ref`: "view" and its id, "app" and "<workspace>/<key>".
    surface_kind = models.CharField(max_length=20)
    surface_ref = models.CharField(max_length=200)
    # `declarations.Surface.revision`: a view's version, an app's live version id.
    revision = models.BigIntegerField(null=True, blank=True)
    writes_used = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
    hard_limit_at = models.DateTimeField()


class ConnectionSurfaceShare(models.Model):
    """An agent's connection serving one project's or app's screens, as its admin approved.

    `approved_reads` and `approved_writes` are the declared lines the admin approved, read from the
    surface when they did so (`services.share_with_surface`), never from what a client sent. A
    screen's call stops when its declaration holds a line they don't cover. An agent's connection
    only: a person's own account is never used by someone else."""

    PROJECT = "project"
    APP = "app"
    KINDS = [(PROJECT, "Project"), (APP, "App")]

    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name="surface_shares")
    kind = models.CharField(max_length=10, choices=KINDS)
    # `declarations.Surface.share_key`: a project's id, or an app's "<workspace>/<key>".
    ref = models.CharField(max_length=200)
    approved_reads = models.JSONField(default=list, blank=True)
    approved_writes = models.JSONField(default=list, blank=True)
    # SET_NULL for the same reason as `ConnectionGrant.granted_by`.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "kind", "ref"], name="connectionsurfaceshare_unique"),
        ]


class TemporaryAccess(models.Model):
    """A person's short-lived access token, lent to one agent for turns that person starts.

    The other way to lend an account, for a clean-up rather than a standing connection: no refresh
    token is ever asked for, so nothing can renew it, and it dies with the token (about an hour at
    Google). No vault key or passkey: the provider's own consent screen is the person's yes.

    The price, chosen on purpose: unlike a `Connection`, canopy-web can read this one. `token` is
    encrypted with canopy-web's own key (`apps.common.encryption`), and it hands the token to the
    runner holding the turn, so a hacked canopy-web could use or redirect a token while it lives.
    What it cannot do is outlive that token or mint another. Ended or run out, the row is deleted.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    app = models.ForeignKey(ConnectionApp, on_delete=models.CASCADE, related_name="temporary_accesses")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="+")
    provider = models.CharField(max_length=40)
    account_id = models.CharField(max_length=255)
    account_label = models.CharField(max_length=255, blank=True, default="")
    scopes = models.TextField(blank=True, default="")
    token = models.TextField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One per person, agent and provider: a new sign-in replaces it, so which token a
            # turn gets is never a guess.
            models.UniqueConstraint(fields=["user", "agent", "provider"], name="one_temporary_access_per_lend"),
        ]

    def __str__(self) -> str:
        return f"{self.provider} {self.account_label} for {self.agent_id} until {self.expires_at}"
