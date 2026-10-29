"""Server-held OAuth connections to third-party providers (Salesforce first).

A `Connection` is one grant canopy holds so work can be done AS an owner: a
person (their CLI, their sessions) or an agent (its turns). Exactly one owner,
never both, and there is no fallback between them.

Tokens are Fernet ciphertext (`apps.common.encryption`); a provider may rotate
the refresh token on every refresh, so only canopy-web refreshes, under a row
lock (see the service layer). What differs between providers lives in
`providers/`, never here: `provider` is just the registry key.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Connection(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
        related_name="connections",
    )
    agent = models.ForeignKey(
        "agents.Agent", null=True, blank=True, on_delete=models.CASCADE,
        related_name="connections",
    )
    provider = models.CharField(max_length=40)
    # The provider's stable id for the account (Salesforce: "<org id>/<user id>").
    account_id = models.CharField(max_length=255)
    # Display only — never used for authorization.
    account_label = models.CharField(max_length=255, blank=True, default="")
    # The account's own API base, for a provider with one per account (Salesforce);
    # empty where the provider's API host is fixed.
    instance_url = models.URLField(blank=True, default="")
    # What the provider GRANTED (its `scope`), space-separated; what was asked for
    # when it did not say.
    scopes = models.CharField(max_length=500, blank=True, default="")
    refresh_token_enc = models.TextField(blank=True, default="")
    access_token_enc = models.TextField(blank=True, default="")
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    # How long the last access token lived when issued (the provider's `expires_in`,
    # or the configured lifetime): what a caller's `min_life` is capped against.
    access_token_seconds = models.PositiveIntegerField(null=True, blank=True)
    # Set when a refresh is rejected; cleared on every successful refresh.
    refresh_failed_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    # Who completed the sign-in. SET_NULL: an agent's connection outlives the
    # admin who connected it (deleting that person must not take the agent's
    # Salesforce identity with them).
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
            models.UniqueConstraint(
                fields=["user", "provider", "account_id"], name="connection_unique_user_account",
            ),
            models.UniqueConstraint(
                fields=["agent", "provider", "account_id"], name="connection_unique_agent_account",
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
        """True when the stored grant cannot mint an access token any more."""
        return not self.refresh_token_enc or self.refresh_failed_at is not None


class ConnectionGrant(models.Model):
    """An AGENT's connection used by another agent.

    Today's case: Eva's Salesforce user (eva@dimagi-ai.com), used by Echo. Only
    for agent-owned connections; a person's connection is never granted to an
    agent. Granting needs admin of both agents (enforced where it is created).
    """

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

    def clean(self) -> None:
        if self.connection.agent_id is None:
            raise ValidationError("Only an agent-owned connection can be granted to another agent.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)
