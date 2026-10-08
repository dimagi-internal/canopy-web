"""Walkthrough model — one shareable HTML slideshow or MP4 video."""
import secrets
import uuid

from django.conf import settings
from django.db import models


class Walkthrough(models.Model):
    KIND_HTML = "html"
    KIND_VIDEO = "video"
    KIND_CHOICES = [
        (KIND_HTML, "HTML"),
        (KIND_VIDEO, "Video"),
    ]

    VISIBILITY_PRIVATE = "private"
    VISIBILITY_LINK = "link"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private (dimagi only)"),
        (VISIBILITY_LINK, "Link (anyone with token)"),
    ]

    # Artifact roles within a DDD run. Optional — when absent, the run
    # aggregator derives a role from ``kind`` (video->clip, html->deck).
    ROLE_HERO_VIDEO = "hero_video"
    ROLE_DECK = "deck"
    ROLE_DOCS = "docs"
    ROLE_CLIP = "clip"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    project_slug = models.CharField(
        max_length=200, blank=True, null=True, db_index=True
    )
    # DDD-run grouping. A run_id looks like "<narrative_slug>-YYYY-MM-DD-NNN" and ties
    # this artifact to its sibling video/deck/narrative. One-off (non-DDD)
    # uploads leave these null and never surface in the DDD section.
    run_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    # Narrative slug (matches run_state.yaml's `narrative_slug`). Falls back to
    # narrative_slug_from_run_id(run_id) when blank but run_id is set.
    narrative_slug = models.CharField(max_length=200, blank=True, null=True)
    # Artifact role: hero_video | deck | docs | clip. Free-form (not a DB-level
    # choices enum) so the plugin can evolve it; the aggregator tolerates blanks.
    role = models.CharField(max_length=20, blank=True, null=True)
    # The narrative version (ReviewRequest.id) this run rendered. Stamped by the
    # plugin at upload; lets a run link to its exact story version instead of
    # being matched by run_id. Null for one-off / legacy uploads.
    narrative_review_id = models.UUIDField(blank=True, null=True, db_index=True)
    # One cut of a `style: recorded` narrative (canopy#796): the recipe's
    # ``cuts[].id``. A recorded narrative renders one mp4 per cut, so a version
    # holds one video PER CUT rather than one video — re-uploading a cut id
    # replaces that cut's video (latest wins). Blank for every other upload.
    # The cuts live only in canopy's recipe, never on canopy-web, so the upload
    # also carries the cut's scene ids (the narration item ids it plays, in
    # order): that is how the review page puts each video next to its words.
    cut_id = models.CharField(max_length=100, blank=True, default="", db_index=True)
    cut_scene_ids = models.JSONField(default=list, blank=True)
    owner =models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="walkthroughs",
    )
    # The tenant that owns this walkthrough. A walkthrough is its OWN tenant root
    # (it may carry a project_slug, but tenancy is by workspace, not project).
    # Nullable for migration safety; the API always assigns one (the request's
    # workspace, else the default workspace when unspecified).
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="walkthroughs",
        help_text=(
            "The tenant that owns this walkthrough. Nullable for migration "
            "safety; the API always assigns one (default workspace when "
            "unspecified)."
        ),
    )
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE
    )
    share_token = models.CharField(
        max_length=64, blank=True, null=True, unique=True
    )
    drive_file_id = models.CharField(max_length=128)
    drive_folder_id = models.CharField(max_length=128)
    content_type = models.CharField(max_length=64)
    size_bytes = models.BigIntegerField()
    duration_sec = models.IntegerField(null=True, blank=True)
    # Companion links shown on the viewer page. Each entry is
    # {"label": str, "url": str, "kind": "narrative" | "companion" | "reference"}.
    #   narrative  — the design narrative / spec that generated this walkthrough
    #   companion  — the sibling artifact (e.g. the still-frame deck for a video)
    #   reference  — a destination shown in the demo the viewer can go explore
    # Authored by the uploader (the DDD loop populates them from the spec); the
    # viewer groups them into "Narrative", "Still-frame walkthrough", and
    # "Explore in the app" sections.
    links = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["project_slug", "-created_at"]),
            models.Index(fields=["owner", "-created_at"]),
            models.Index(fields=["run_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.title} ({self.kind})"

    def ensure_share_token(self) -> str:
        """Mint a share token if none exists. Returns the token."""
        if not self.share_token:
            self.share_token = secrets.token_urlsafe(24)
            self.save(update_fields=["share_token", "updated_at"])
        return self.share_token

    def rotate_share_token(self) -> str:
        """Replace the existing share token with a fresh one."""
        self.share_token = secrets.token_urlsafe(24)
        self.save(update_fields=["share_token", "updated_at"])
        return self.share_token

    def token_matches(self, token: str | None) -> bool:
        """Constant-time check that ``token`` grants anonymous public access.

        True only when the walkthrough is public (visibility=link), a token
        has been minted, and the presented token matches. Empty/absent on
        either side never matches.
        """
        return bool(
            self.visibility == self.VISIBILITY_LINK
            and self.share_token
            and token
            and secrets.compare_digest(
                self.share_token.encode("utf-8"), token.encode("utf-8")
            )
        )

    def readable_by(self, request) -> bool:
        """The read gate for detail + content: a MEMBER of the walkthrough's
        workspace, or anyone presenting a matching ?t=<share_token> (visibility=
        link). Being merely authenticated is NOT enough — that was a cross-workspace
        read leak. A null-workspace row has no members: only its token reads it
        (it used to fall back to any authenticated user — NULL-means-allow)."""
        from apps.workspaces import services as wsvc

        member = (
            self.workspace_id is not None
            and self.workspace_id in wsvc.request_workspace_slugs(request)
        )
        return member or self.token_matches(request.GET.get("t"))
