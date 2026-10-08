"""Models for shared Claude Code session transcripts.

A ``Session`` is one uploaded ``.jsonl`` transcript, decomposed into ordered
``Message`` rows. Sharing is link-by-default: every upload mints a
``ShareToken`` so the returned ``/share/<token>`` URL works immediately.
The owner can rotate (invalidate the old link) or revoke (make it private).

This is the generic counterpart to ace-web's opportunity-scoped sessions app:
the parser + message shape are ported verbatim, but the ACE opp-linkage,
multiplayer participants, drafts, and live-streaming machinery are dropped —
uploaded transcripts are static and single-owner.
"""
from __future__ import annotations

import secrets

from django.conf import settings
from django.db import IntegrityError, models, transaction


def generate_slug() -> str:
    """8-character URL-safe random slug for a session."""
    return secrets.token_urlsafe(6)[:8]


def generate_share_token() -> str:
    """24-byte URL-safe random token (~32 chars) for a public share link."""
    return secrets.token_urlsafe(24)


class Session(models.Model):
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_LINK = "link"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private (dimagi only)"),
        (VISIBILITY_LINK, "Link (anyone with token)"),
    ]

    slug = models.CharField(max_length=32, unique=True, default=generate_slug)
    title = models.CharField(max_length=500, blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shared_sessions",
    )
    # The workspace it is shared from — its page is /w/<workspace>/share/<token>
    # (owner decision, 2026-10-08: nothing on canopy outside a workspace). Set at
    # upload from a workspace the uploader is in; `0005` homed the rows that
    # predate it. Nullable only for a row whose owner belongs to no workspace,
    # which the flat /share/<token> link still serves in place.
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    project_slug = models.CharField(
        max_length=200, blank=True, null=True, db_index=True
    )
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_LINK
    )

    # The Claude CLI session id (from the `init` line). Used to dedup repeated
    # uploads of the same transcript — a second upload returns the existing
    # session instead of creating a duplicate.
    cli_session_id = models.CharField(max_length=200, blank=True, default="")

    # Best-effort secret scrub stats, stamped at ingest.
    redaction_count = models.IntegerField(default=0)

    # Ingest provenance (audit + UI metadata).
    source_filename = models.CharField(max_length=500, blank=True, default="")
    raw_bytes = models.BigIntegerField(default=0)
    line_count = models.IntegerField(default=0)

    # When the original Claude session actually ran (first/last event timestamps
    # from the raw transcript). Distinct from created_at, which is the upload
    # time. Nullable — older uploads and transcripts without timestamps have none.
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    # Estimated *active* duration in seconds — inter-event gaps capped at 30 min
    # so idle time doesn't inflate it (the honest "how long", vs the wall-clock
    # started_at→ended_at span). Nullable for older/timestamp-less uploads.
    active_seconds = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "shared_sessions"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["project_slug", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.slug}: {self.title or '(untitled)'}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = generate_slug()
        # Each attempt runs in its own savepoint so a duplicate-slug
        # IntegrityError rolls back only this attempt and doesn't poison an
        # enclosing transaction.
        for _ in range(5):
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not self.pk:
                    self.slug = generate_slug()
                    continue
                raise
        raise IntegrityError("Could not generate a unique slug after 5 attempts")

    def active_token(self) -> "ShareToken | None":
        return (
            self.share_tokens.filter(revoked_at__isnull=True)
            .order_by("-created_at")
            .first()
        )

    def ensure_share_token(self, created_by) -> "ShareToken":
        """Return the active token, minting one (and flipping to link) if none."""
        token = self.active_token()
        if token is None:
            token = ShareToken.objects.create(session=self, created_by=created_by)
            if self.visibility != self.VISIBILITY_LINK:
                self.visibility = self.VISIBILITY_LINK
                self.save(update_fields=["visibility", "updated_at"])
        return token

    def rotate_share_token(self, created_by) -> "ShareToken":
        """Revoke any active token and mint a fresh one. Returns the new token."""
        from django.utils import timezone

        self.share_tokens.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        token = ShareToken.objects.create(session=self, created_by=created_by)
        if self.visibility != self.VISIBILITY_LINK:
            self.visibility = self.VISIBILITY_LINK
            self.save(update_fields=["visibility", "updated_at"])
        return token

    def revoke_sharing(self) -> None:
        """Revoke all active tokens and mark the session private."""
        from django.utils import timezone

        self.share_tokens.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        if self.visibility != self.VISIBILITY_PRIVATE:
            self.visibility = self.VISIBILITY_PRIVATE
            self.save(update_fields=["visibility", "updated_at"])


class Message(models.Model):
    ROLE_CHOICES = [
        ("user", "User"),
        ("assistant", "Assistant"),
        ("system", "System"),
        ("tool_use", "Tool use"),
        ("tool_result", "Tool result"),
    ]

    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="messages"
    )
    turn_index = models.IntegerField()
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.JSONField()
    plaintext = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shared_session_messages"
        ordering = ["session_id", "turn_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "turn_index"],
                name="unique_shared_session_turn",
            ),
        ]

    def __str__(self):
        return f"[{self.session_id}] turn {self.turn_index} ({self.role})"


class ShareToken(models.Model):
    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="share_tokens"
    )
    token = models.CharField(max_length=64, unique=True, default=generate_share_token)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shared_session_share_tokens",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shared_session_share_tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Token {self.token[:8]}… for session {self.session_id}"


class SessionArc(models.Model):
    """An ordered group of shared sessions presented as one read-only page.

    The original motivating case: a feature gets built across several Claude
    sessions (sometimes across machine accounts). Each session shares cleanly on
    its own, but the *arc* — the whole build, in order — is the thing worth
    sending a teammate. An arc references existing ``Session`` rows (their
    turn-syntheses), so a session can appear in its own link AND in an arc
    without re-uploading.

    Sharing mirrors ``Session``: link-by-default via its own ``ArcShareToken``,
    served from the same ``/api/share/<token>`` surface (so the public read,
    SPA route, and login-middleware exemption are all reused unchanged).
    """

    VISIBILITY_PRIVATE = Session.VISIBILITY_PRIVATE
    VISIBILITY_LINK = Session.VISIBILITY_LINK
    VISIBILITY_CHOICES = Session.VISIBILITY_CHOICES

    slug = models.CharField(max_length=32, unique=True, default=generate_slug)
    title = models.CharField(max_length=500, blank=True, default="")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shared_session_arcs",
    )
    # The workspace it is shared from — its page is /w/<workspace>/share/<token>
    # (owner decision, 2026-10-08: nothing on canopy outside a workspace). Set at
    # upload from a workspace the uploader is in; `0005` homed the rows that
    # predate it. Nullable only for a row whose owner belongs to no workspace,
    # which the flat /share/<token> link still serves in place.
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    project_slug = models.CharField(
        max_length=200, blank=True, null=True, db_index=True
    )
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_LINK
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "shared_session_arcs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["project_slug", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.slug}: {self.title or '(untitled arc)'}"

    def save(self, *args, **kwargs):
        # Slug-collision-retry, mirroring Session.save (each attempt in its own
        # savepoint so a duplicate-slug rollback doesn't poison an outer txn).
        if not self.slug:
            self.slug = generate_slug()
        for _ in range(5):
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                if not self.pk:
                    self.slug = generate_slug()
                    continue
                raise
        raise IntegrityError("Could not generate a unique slug after 5 attempts")

    def active_token(self) -> "ArcShareToken | None":
        return (
            self.share_tokens.filter(revoked_at__isnull=True)
            .order_by("-created_at")
            .first()
        )

    def ensure_share_token(self, created_by) -> "ArcShareToken":
        token = self.active_token()
        if token is None:
            token = ArcShareToken.objects.create(arc=self, created_by=created_by)
            if self.visibility != self.VISIBILITY_LINK:
                self.visibility = self.VISIBILITY_LINK
                self.save(update_fields=["visibility", "updated_at"])
        return token

    def rotate_share_token(self, created_by) -> "ArcShareToken":
        from django.utils import timezone

        self.share_tokens.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        token = ArcShareToken.objects.create(arc=self, created_by=created_by)
        if self.visibility != self.VISIBILITY_LINK:
            self.visibility = self.VISIBILITY_LINK
            self.save(update_fields=["visibility", "updated_at"])
        return token

    def revoke_sharing(self) -> None:
        from django.utils import timezone

        self.share_tokens.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        if self.visibility != self.VISIBILITY_PRIVATE:
            self.visibility = self.VISIBILITY_PRIVATE
            self.save(update_fields=["visibility", "updated_at"])


class SessionArcItem(models.Model):
    """One member session within an arc, at a fixed position with an optional
    section heading (defaults to the session's own title at render time)."""

    arc = models.ForeignKey(
        SessionArc, on_delete=models.CASCADE, related_name="items"
    )
    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="arc_memberships"
    )
    position = models.IntegerField()
    heading = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        db_table = "shared_session_arc_items"
        ordering = ["arc_id", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["arc", "position"], name="unique_arc_position"
            ),
            models.UniqueConstraint(
                fields=["arc", "session"], name="unique_arc_session"
            ),
        ]

    def __str__(self):
        return f"[{self.arc_id}] #{self.position} → session {self.session_id}"


class ArcShareToken(models.Model):
    arc = models.ForeignKey(
        SessionArc, on_delete=models.CASCADE, related_name="share_tokens"
    )
    token = models.CharField(max_length=64, unique=True, default=generate_share_token)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="shared_session_arc_share_tokens",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shared_session_arc_share_tokens"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Token {self.token[:8]}… for arc {self.arc_id}"
