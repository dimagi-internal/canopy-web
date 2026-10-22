"""The MCP sign-in's records: registered clients, one-time codes, and grants.

Moved from apps/tokens with their tables (`oauth_clients`,
`oauth_authorization_codes`, `oauth_grants`) — state-only migrations, no data moved.
The access tokens a grant mints are ordinary `tokens.PersonalToken`s
(`PersonalToken.oauth_grant`).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class OAuthClient(models.Model):
    """An MCP client that registered itself (RFC 7591 dynamic registration).

    Registration grants NOTHING: a client holds no secret (public client, PKCE
    only) and reaches nothing until a signed-in person approves it on the
    consent page, which is why registration can be open. What it buys is that
    the consent page can name the client and pin its redirect URIs.
    """

    client_id = models.CharField(max_length=64, unique=True)
    client_name = models.CharField(max_length=200, blank=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "oauth_clients"

    def __str__(self):
        return f"OAuthClient {self.client_name or self.client_id}"

    @property
    def display_name(self) -> str:
        return self.client_name or "An MCP client"


class OAuthAuthorizationCode(models.Model):
    """A one-time code from the consent page, exchanged at `/oauth/token`.

    Only the hash is stored, it lives ten minutes, and it is bound to the
    client, the exact redirect URI and a PKCE challenge — so a code intercepted
    on its way back to the client is useless without the verifier.
    """

    code_hash = models.CharField(max_length=64, unique=True)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="codes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="+")
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=128)
    scope = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    #: The grant the exchange created — so a replayed code can revoke it (RFC 6749 §4.1.2).
    grant = models.ForeignKey("oauth_server.OAuthGrant", null=True, blank=True,
                              on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "oauth_authorization_codes"


class OAuthGrant(models.Model):
    """One person's approval of one MCP client — a "connected app".

    Holds the current refresh token (hashed; rotated on every use) and owns the
    short-lived access tokens minted from it, which are ordinary
    `PersonalToken`s, so REST and MCP authenticate them exactly as they do a
    hand-minted token. Revoking the grant revokes all of them.
    """

    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="oauth_grants")
    scope = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    refresh_token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    #: The refresh token this one replaced. Presenting it again means two parties
    #: hold the grant's refresh token — the grant is revoked (reuse detection).
    previous_refresh_hash = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    refresh_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "oauth_grants"
        ordering = ["-created_at"]

    def __str__(self):
        return f"OAuthGrant {self.client_id} for {self.user_id}"

    def revoke(self) -> None:
        now = timezone.now()
        self.revoked_at = now
        self.refresh_token_hash = None
        self.save(update_fields=["revoked_at", "refresh_token_hash"])
        self.access_tokens.filter(revoked_at__isnull=True).update(revoked_at=now)
