"""A request for access to canopy (closed to Dimagi and its partners), from the
public site's request-access form.

Deliberately NOT a `WorkspaceAccessRequest`: that needs an account, names a
workspace, and grants a membership when approved. A beta request comes from
someone with no account, names no workspace, and creates no user. It is answered
on its own page (`/beta-requests/:id`, linked from the email): the reviewer picks
a workspace and a role, and canopy sends an ordinary invite — which is still the
only thing that lets anyone in.
"""
from django.conf import settings
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

    PENDING, INVITED, DECLINED = "pending", "invited", "declined"
    STATUS_CHOICES = [(PENDING, "Pending"), (INVITED, "Invited"), (DECLINED, "Declined")]
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    # The answer, once there is one. `invite` is the invite sent on approval;
    # `workspace` and `role` are kept as well so the record still reads after the
    # invite is revoked or deleted with its workspace.
    workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    role = models.CharField(max_length=16, blank=True, default="")
    invite = models.ForeignKey("workspaces.WorkspaceInvite", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["email", "created_at"])]

    def __str__(self) -> str:
        return f"{self.email} ({self.created_at:%Y-%m-%d})"
