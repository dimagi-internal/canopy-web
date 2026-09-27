from django.db import models
from django.utils import timezone


class SeenJti(models.Model):
    """A ``jti`` already used, so a signed statement works exactly once.

    One table for every kind (client assertion, grant, DPoP proof), keyed by kind
    plus a hash of the value. In the database rather than a cache because single
    use has to hold across every worker and has to fail CLOSED: an insert that
    cannot happen refuses the request, where a cache that silently drops writes
    would quietly allow replays.
    """

    key = models.CharField(max_length=200, unique=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "canopy_host_seen_jti"

    def __str__(self) -> str:
        return self.key


class DelegatedToken(models.Model):
    """An access token issued to canopy for a visitor (the jwt-bearer grant).

    Its OWN table, deliberately — not an OAuth library's access-token table. A
    library that authenticates a host's REST API with any live row in its table
    would otherwise open that whole API to canopy. Here only the MCP verifier
    reads it.

    The raw token is never stored, only its SHA-256. ``cnf_jkt`` is the DPoP key
    it is bound to; there is no refresh token.
    """

    token_checksum = models.CharField(max_length=64, unique=True)
    #: The host's own id for the visitor (the ID-JAG's ``sub``).
    subject = models.CharField(max_length=255, db_index=True)
    client_id = models.CharField(max_length=255)
    #: RFC 8693 ``act.sub`` — who acts for the visitor.
    actor = models.CharField(max_length=255, blank=True)
    scope = models.CharField(max_length=255)
    cnf_jkt = models.CharField(max_length=64)
    grant_jti = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "canopy_host_delegated_token"

    def __str__(self) -> str:
        return f"Delegated token for {self.subject} via {self.client_id}"
