"""What a by-name membership row is, wherever it lives (`VaultMember`, `ProjectMember`)."""
from django.conf import settings
from django.db import models


class NamedMember(models.Model):
    """A person added by name. A row gives `admin` or `member`; `owner` is never stored,
    it comes only from what the person already holds (owning the agent or workspace, or
    leading the project)."""

    OWNER, ADMIN, MEMBER = "owner", "admin", "member"
    ROLE_CHOICES = [(ADMIN, "Admin"), (MEMBER, "Member")]

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=MEMBER)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
