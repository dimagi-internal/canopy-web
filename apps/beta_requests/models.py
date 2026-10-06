"""A request to join canopy's closed beta, from the public site's sign-up form.

Deliberately NOT a `WorkspaceAccessRequest`: that needs an account, names a
workspace, and grants a membership when approved. A beta request is a note to a
person — it grants nothing, creates no user, and is answered by an ordinary invite.
"""
from django.db import models


class BetaRequest(models.Model):
    email = models.EmailField()
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Correlation, not evidence: X-Forwarded-For is client-settable on a direct
    # connection (apps/tokens/audit.client_ip). Used for the per-address limit.
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    # The notification is best-effort and recorded here, never raised: a request
    # is kept even when the mail to its reader fails. `sent` | `not_configured` |
    # `failed` | `skipped` (a repeat of a recent request from the same address).
    notify_result = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "created_at"])]

    def __str__(self) -> str:
        return f"{self.email} ({self.created_at:%Y-%m-%d})"
