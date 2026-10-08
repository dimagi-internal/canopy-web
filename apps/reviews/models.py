"""ReviewRequest model — one pending-or-resolved human review gate."""
import secrets
import uuid

from django.conf import settings
from django.db import models


class ReviewRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_RESOLVED = "resolved"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_RESOLVED, "Resolved"),
    ]

    VISIBILITY_PRIVATE = "private"
    VISIBILITY_LINK = "link"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private (dimagi only)"),
        (VISIBILITY_LINK, "Public (anyone with the link)"),
    ]
    # Visibility semantics (tokenless — the UUID in the URL is the only secret):
    #   private — readable only by dimagi-authenticated users (the app is dimagi-OAuth
    #             gated).  Anonymous callers get a 404 (existence is not leaked).
    #   link    — readable by anyone with the URL, no session required.  This is how the
    #             canopy orchestrator URL is shared (e.g. posted to Slack).
    # Submitting a decision ALWAYS requires a dimagi login regardless of visibility —
    # public-readable does not grant anonymous write.

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run_id = models.CharField(max_length=255, db_index=True)
    # DDD narrative identity (the stable narrative_id) + version. A ReviewRequest
    # that carries a narrative IS a narrative version; `version` is monotonic per
    # `narrative_slug`, assigned server-side on create. `narrative_slug` falls back to the
    # run_id slug when the client doesn't send it explicitly.
    narrative_slug = models.CharField(max_length=200, blank=True, null=True, db_index=True)
    version = models.IntegerField(default=1, db_index=True)
    gate = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True
    )
    request_json = models.JSONField()
    response_json = models.JSONField(null=True, blank=True)
    # Suggestions from external (share-token) reviewers who cannot resolve the gate.
    # Each entry: {response_json, name, created_at}. The owner reviews + accepts.
    suggestions_json = models.JSONField(default=list, blank=True)
    share_token = models.CharField(max_length=64, blank=True, null=True, unique=True)
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_requests",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviews",
        help_text=(
            "The tenant that owns this review. Nullable for migration safety; the "
            "API always assigns one (default workspace when unspecified). A review "
            "with a narrative_slug is a DDD narrative version, but it is still its "
            "own tenant root via this FK."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["run_id", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["narrative_slug", "version"]),
        ]

    def __str__(self):
        return f"ReviewRequest({self.run_id!r}, gate={self.gate!r}, status={self.status})"

    @classmethod
    def next_version(cls, narrative_slug: str) -> int:
        """Next monotonic narrative version for a narrative_slug (1-based)."""
        if not narrative_slug:
            return 1
        latest = (
            cls.objects.filter(narrative_slug=narrative_slug)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
        )
        return (latest or 0) + 1

    def ensure_share_token(self) -> str:
        """Mint a share token if none exists. Returns the token."""
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(24)
            self.save(update_fields=["share_token"])
        return self.share_token

    def rotate_share_token(self) -> str:
        """Replace the existing share token with a fresh one."""
        self.share_token = secrets.token_urlsafe(24)
        self.save(update_fields=["share_token"])
        return self.share_token

    def add_suggestion(
        self, response_json: dict, name: str | None = None, email: str | None = None
    ) -> int:
        """Append an external reviewer's suggestion. Does NOT resolve the gate —
        the review stays pending for the owner to review + accept. Returns the new
        suggestion count."""
        from django.utils import timezone

        items = list(self.suggestions_json or [])
        items.append(
            {
                "response_json": response_json,
                "name": (name or "").strip() or None,
                "email": (email or "").strip() or None,
                "created_at": timezone.now().isoformat(),
            }
        )
        self.suggestions_json = items
        self.save(update_fields=["suggestions_json"])
        return len(items)
