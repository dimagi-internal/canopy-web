"""Storyboards — an ordered arc of DDD narratives, shareable as one link.

Product tier: this curates DDD narratives, which are product. It may import
framework freely (``feedback``, ``workspaces``); nothing framework-side may
import it.

Why this exists: a single narrative stopped being the unit of the story. An arc
of four or five narratives, grouped into acts, is what you actually send to a
prospective user — and canopy-web had no object above ``narrative`` (which is
not even a table; the run aggregation infers it from a run_id slug at read
time). So there was nothing to share and nothing for feedback to attach to.

The page FOLLOWS each narrative's current release rather than freezing a run id,
so an emailed link never goes stale. What makes that safe is that every
``Feedback`` row records the version it was left against — otherwise a comment
would silently lose its anchor the moment the narrative moved.
"""
from __future__ import annotations

import secrets

from django.db import models


class Storyboard(models.Model):
    CAP_READ = "read"
    CAP_COMMENT = "comment"
    CAP_SUGGEST = "suggest"
    CAP_CHOICES = [
        (CAP_READ, "Read only"),
        (CAP_COMMENT, "Read + comment"),
        (CAP_SUGGEST, "Read + comment + suggest edits"),
    ]

    slug = models.SlugField(max_length=100)
    title = models.CharField(max_length=300)
    lede = models.TextField(blank=True, default="")
    """One paragraph under the title — what the whole arc is for."""

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="storyboards",
    )

    share_token = models.CharField(max_length=64, blank=True, null=True, unique=True)
    capability = models.CharField(max_length=16, choices=CAP_CHOICES, default=CAP_READ)
    """What the share link grants. ONE grant per board, not per token — if you
    later need "Ellyn comments, Sophie suggests", that is a second token model
    and should be built then, not anticipated now."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Per-workspace, not global: two tenants may each have a `supply`
            # board and neither should block the other.
            models.UniqueConstraint(
                fields=["workspace", "slug"], name="uniq_storyboard_workspace_slug"
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"storyboard:{self.workspace_id}:{self.slug}"

    # -- sharing ------------------------------------------------------------
    # Mirrors apps/walkthroughs/models.py rather than inventing a second scheme.

    def ensure_share_token(self) -> str:
        """Mint a share token if none exists. Returns the token."""
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(24)
            self.save(update_fields=["share_token", "updated_at"])
        return self.share_token

    def rotate_share_token(self) -> str:
        """Replace the share token with a fresh one, killing every link already
        sent. The artifact itself is untouched."""
        self.share_token = secrets.token_urlsafe(24)
        self.save(update_fields=["share_token", "updated_at"])
        return self.share_token

    def token_matches(self, token: str | None) -> bool:
        """Constant-time check that ``token`` grants anonymous access.

        Empty or absent on either side never matches. A caller presenting a
        wrong token must be 404'd, not 403'd — a 403 would confirm the board
        exists, which is the existence leak walkthroughs already avoid.
        """
        return bool(
            self.share_token
            and token
            and secrets.compare_digest(
                self.share_token.encode("utf-8"), token.encode("utf-8")
            )
        )

    def grants(self, capability: str, token: str | None) -> bool:
        """True when a token-bearing anonymous caller may do ``capability``."""
        if not self.token_matches(token):
            return False
        ladder = [self.CAP_READ, self.CAP_COMMENT, self.CAP_SUGGEST]
        return ladder.index(self.capability) >= ladder.index(capability)


class Act(models.Model):
    """One act of the arc: a title, connective prose, and ordered entries."""

    storyboard = models.ForeignKey(Storyboard, on_delete=models.CASCADE, related_name="acts")
    title = models.CharField(max_length=300)
    prose = models.TextField(blank=True, default="")
    """The connective tissue — why this act follows the last one. This is the
    thing a linear telling cannot carry."""
    position = models.IntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"act:{self.storyboard_id}:{self.position}:{self.title[:40]}"


class Entry(models.Model):
    """One narrative's place in an act."""

    act = models.ForeignKey(Act, on_delete=models.CASCADE, related_name="entries")
    narrative_slug = models.CharField(max_length=200)
    """A STRING, like ``Feedback.target_ref`` — a narrative is inferred at read
    time, not a table, so there is nothing to point an FK at."""
    position = models.IntegerField(default=0)

    pinned_run_id = models.CharField(max_length=200, blank=True, default="")
    """Normally blank, and it should stay that way: the entry resolves to the
    narrative's CURRENT release so a shared link never goes stale. This exists
    for the one case that needs it — holding an entry on a known-good run while
    that narrative is mid-redraft."""

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"entry:{self.act_id}:{self.position}:{self.narrative_slug}"
