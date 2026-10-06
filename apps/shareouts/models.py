from django.conf import settings
from django.db import models


class Shareout(models.Model):
    """A dated, teammate-facing work briefing.

    One row per project per period; a row with `project_slug=None` is the
    cross-project roll-up for that period. Posted by the `canopy:shareout`
    skill. Re-running the same period from the same source replaces the
    prior rows (see `apps.shareouts.services.upsert_shareouts`), so the feed
    is a clean log rather than an append pile.
    """

    # The repo slug this briefing is about — a bare string, the same shape
    # `Walkthrough.project_slug` uses. It was an FK to the workbench project
    # registry until that was retired (shareouts/0008 copied the slugs); any
    # well-formed slug is accepted now, because there is no registry to check.
    project_slug = models.CharField(
        max_length=200,
        blank=True,
        null=True,
        db_index=True,
        help_text="Null = cross-project roll-up for the period.",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        related_name="shareouts",
        null=True,
        blank=True,
        help_text=(
            "The tenant that owns this shareout. Shareout is its own tenant root "
            "(project_slug is orthogonal). Nullable for migration safety; the "
            "API always assigns one (default workspace when unspecified)."
        ),
    )
    # Timestamps (not dates): a shareout is rarely run on a clean day boundary,
    # so the window is precise to the second and consecutive shareouts chain
    # exactly (next.period_start == prev.period_end).
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default="")
    content = models.TextField()
    links = models.JSONField(default=list, blank=True)
    # Curated highlight links (subset of all_prs, most relevant first).
    all_prs = models.JSONField(default=list, blank=True)
    # Every PR in the window for this project: [{number,title,url,state}].
    author = models.CharField(max_length=100, blank=True, default="")
    # The agent that ASSEMBLED this briefing on the author's behalf (slug, e.g.
    # "eva"), or "" when a human ran the shareout themselves. Attribution stays
    # with `author` (whose work it's about) — this only records the producer,
    # so the feed can show a subtle "produced by <agent>" byline. Deliberately
    # NOT part of the idempotency group (see services.upsert_shareouts): it
    # rides along, so a re-post from the same source still replaces cleanly.
    produced_by_agent = models.CharField(max_length=80, blank=True, default="")
    # The canopy login that POSTED the row — distinct from `author`, which is a
    # free string the poster supplies. This is what "your own shareouts" means
    # when a re-post replaces a period or a clear runs, so one editor cannot
    # wipe or overwrite a teammate's briefing. NULL on rows that predate it;
    # only a workspace owner may clear those.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end", "-created_at"]
        indexes = [
            models.Index(fields=["-period_end", "-period_start"]),
        ]

    def __str__(self):
        scope = self.project_slug or "roll-up"
        return f"shareout:{scope}:{self.period_start}..{self.period_end}"
