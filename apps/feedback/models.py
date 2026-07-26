"""Feedback on a thing, from a person, via a channel.

Framework tier. Generic over its target on PURPOSE: ``target_kind`` +
``target_ref`` are strings, never an FK. A DDD narrative is not even a table —
the product tier infers it from a ``run_id`` slug at read time — and an FK to a
product model would break the one-way framework→product rule. Same discipline
``Item`` follows: carry your own text, resolve nothing.

Feedback is INPUT TO A DECISION, not work. This app deliberately emits no
signal: no Item, no push, no timeline event. A turn reads the pool when the
owner is ready, clusters it across channels, and proposes what to do with each
piece. Auto-promoting would rebuild the queue-grooming step the inbox redesign
removed, and would let an external reviewer enqueue work directly.

canopy-web is not an integration hub. Email and Google-Doc feedback land here
because an AGENT reads them and POSTs — there is no poller and no third-party
credential in this app, which is exactly what lets it stay generic over
``channel``.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class Feedback(models.Model):
    KIND_COMMENT = "comment"
    KIND_SUGGESTION = "suggestion"
    KIND_CHOICES = [(KIND_COMMENT, "Comment"), (KIND_SUGGESTION, "Suggestion")]

    CHANNEL_WEB = "web"
    CHANNEL_EMAIL = "email"
    CHANNEL_GDOC = "gdoc"
    CHANNEL_MANUAL = "manual"
    CHANNEL_API = "api"
    CHANNEL_CHOICES = [
        (CHANNEL_WEB, "Web"),
        (CHANNEL_EMAIL, "Email"),
        (CHANNEL_GDOC, "Google Doc"),
        (CHANNEL_MANUAL, "Manual"),
        (CHANNEL_API, "API"),
    ]

    STATE_NEW = "new"
    STATE_TRIAGED = "triaged"
    STATE_ANSWERED = "answered"
    STATE_DECLINED = "declined"
    STATE_CHOICES = [
        (STATE_NEW, "New"),
        (STATE_TRIAGED, "Triaged"),
        (STATE_ANSWERED, "Answered"),
        (STATE_DECLINED, "Declined"),
    ]

    # ------------------------------------------------------------------ target
    target_kind = models.CharField(max_length=32)
    """``"narrative"`` today, ``"storyboard"`` in L3. A string so this app never
    imports the product app that owns the target."""

    target_ref = models.CharField(max_length=200)
    """The target's slug."""

    target_version = models.IntegerField(null=True, blank=True)
    """The version this feedback was left against. The shared page FOLLOWS each
    narrative's current release, so a comment has to remember the text that
    provoked it — this is what lets the UI say "left against v3, now v5"."""

    anchor_id = models.CharField(max_length=200, blank=True, default="")
    """A stable scene id (L0) or an act id. Blank means the whole target."""

    # -------------------------------------------------------------- the content
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_COMMENT)
    body = models.TextField(blank=True, default="")
    suggested_text = models.TextField(blank=True, default="")
    """Proposed replacement narration when ``kind=suggestion``. It reaches the
    narrative only through a turn the owner fires — never automatically."""

    # --------------------------------------------------------------- the author
    author_name = models.CharField(max_length=200, blank=True, default="")
    author_email = models.CharField(max_length=320, blank=True, default="")
    """Free text: external reviewers have no accounts here and never will."""

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_feedback",
    )
    """The authenticated CALLER, when there was one — the agent's PAT user for an
    ingested email, or the human for a logged-in submit. Never the external
    author, who has no account. Keep the two distinct: conflating them makes
    "who said this" unanswerable for every piece of ingested feedback."""

    # --------------------------------------------------------------- provenance
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, default=CHANNEL_WEB)
    source_ref = models.CharField(max_length=500, blank=True, default="")
    """Opaque provenance for dedupe on re-ingest: an email Message-ID, a doc id +
    comment id. Blank for channels with no natural id (a web submit), which is
    why the uniqueness constraint below excludes blanks."""

    # ------------------------------------------------------------- what happened
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=STATE_NEW)
    disposition_note = models.TextField(blank=True, default="")
    resolved_in_version = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_kind", "target_ref", "state"]),
            models.Index(fields=["channel", "source_ref"]),
        ]
        constraints = [
            # Re-reading a mailbox or a doc must be a no-op. Blank source_ref is
            # EXEMPT: a web submit has no natural id, and two people
            # independently saying "this scene is confusing" are two pieces of
            # feedback, not a duplicate.
            models.UniqueConstraint(
                fields=["channel", "source_ref"],
                condition=~models.Q(source_ref=""),
                name="uniq_feedback_channel_source_ref",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"feedback:{self.target_kind}:{self.target_ref}:{self.pk}"
