"""Pydantic schemas for the /api/ddd surface (read-only run aggregation)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from apps.common.schemas import StrictModel
from apps.walkthroughs.schemas import WalkthroughKind, WalkthroughLink


class NarrativeListItemOut(StrictModel):
    slug: str
    title: str | None = None
    phase: str | None = None
    project_slug: str | None = None
    run_count: int = 0
    latest_at: dt.datetime | None = None
    has_video: bool = False
    has_deck: bool = False
    has_narrative: bool = False


class NarrativeRunOut(StrictModel):
    run_id: str
    created_at: dt.datetime | None = None
    latest_at: dt.datetime | None = None
    status: str | None = None
    gate: str | None = None
    scene_count: int = 0
    has_video: bool = False
    has_deck: bool = False


class NarrativeCutOut(StrictModel):
    """One cut of a ``style: recorded`` narrative and the video pinned to it.

    A recorded narrative renders one mp4 per cut (canopy#796); each is uploaded
    with the recipe's ``cuts[].id`` and the scene ids it plays. Ordered by where
    the cut's first scene falls in the narration."""

    cut_id: str
    title: str
    # Narration item ids (scene ids) this cut plays, in order.
    scene_ids: list[str] = []
    walkthrough_id: uuid.UUID
    video_url: str
    video_viewer_url: str
    duration_sec: int | None = None


class NarrativeVersionOut(StrictModel):
    version: int | None = None
    review_id: str | None = None
    title: str | None = None
    story: str | None = None
    # The per-scene narration for THIS version (same shape as the review's
    # request_json.narration). Carried so the client can render a plain-language
    # before/after (vN -> vN+1) diff without a second fetch per version.
    narration: list[dict[str, Any]] = []
    created_at: dt.datetime | None = None
    gate: str | None = None
    status: str | None = None
    # A narrated video pinned to THIS version (a video walkthrough stamped with
    # this version's review id). None until one is uploaded for the version.
    # For a recorded narrative it is the hero cut (see apps.walkthroughs.pinned).
    video_url: str | None = None
    video_viewer_url: str | None = None
    # A recorded narrative's per-cut videos; empty for an explainer.
    cuts: list[NarrativeCutOut] = []
    runs: list[NarrativeRunOut] = []


class NarrativeStoryOut(StrictModel):
    """The current narrative version, for the narrative header + edit link."""

    review_id: str | None = None
    version: int | None = None
    title: str | None = None
    story: str | None = None
    # The narrated video pinned to the current version, if one was uploaded.
    video_url: str | None = None
    video_viewer_url: str | None = None
    cuts: list[NarrativeCutOut] = []


class NarrativeDetailOut(StrictModel):
    slug: str
    title: str | None = None
    story: str | None = None
    phase: str | None = None
    project_slug: str | None = None
    visibility: Literal["public", "private", "mixed"] = "private"
    current_version: NarrativeStoryOut | None = None
    versions: list[NarrativeVersionOut] = []


class RunArtifactOut(StrictModel):
    id: uuid.UUID
    title: str
    kind: WalkthroughKind
    role: str | None = None
    content_url: str
    viewer_url: str
    duration_sec: int | None = None


class RunArtifactRefOut(StrictModel):
    id: uuid.UUID
    title: str
    kind: WalkthroughKind
    role: str | None = None
    created_at: dt.datetime
    viewer_url: str


class RunNarrativeOut(StrictModel):
    review_id: str | None = None
    version: int | None = None
    run_id: str
    gate: str
    title: str | None = None
    story: str | None = None
    # Sub-shapes live inside ReviewRequest.request_json; kept loosely typed.
    narration: list[dict[str, Any]] = []
    personas: dict[str, Any] = {}
    why_brief: dict[str, Any] | None = None


class RunRecordOut(StrictModel):
    """The run's own record (an ``AgentRun`` run document) — what the runner says
    about the run, instead of a phase guessed from its latest review."""

    agent_slug: str
    project_ext_id: str | None = None
    project_name: str | None = None
    kind: str
    status: str
    current_step: str = ""
    summary: dict[str, Any] = {}
    holder: str = ""
    holder_at: dt.datetime | None = None


class RunPackageOut(StrictModel):
    run_id: str
    narrative_slug: str
    created_at: dt.datetime | None = None
    latest_at: dt.datetime | None = None
    phase: str | None = None
    record: RunRecordOut | None = None
    video: RunArtifactOut | None = None
    # First-class, single-valued run outputs. `slides` is the canopy:walkthrough
    # HTML slideshow (role=deck); `documentation` is the feature docs page
    # (role=docs). They were previously collapsed into one `deck` field that
    # picked docs-then-deck, which hid the slides entirely.
    slides: RunArtifactOut | None = None
    documentation: RunArtifactOut | None = None
    narrative: RunNarrativeOut | None = None
    links: list[WalkthroughLink] = []
    all_artifacts: list[RunArtifactRefOut] = []


class RunReleaseOut(StrictModel):
    """Curated, shareable run release page (the clean public-capable surface).

    A trimmed, outsider-legible slice of a run: title + video + narrative story
    + the live product URLs it used. Artifact URLs are token-appended so an
    anonymous ?t= viewer can stream them. No phase/gate jargon, no artifact
    dump, no edit affordances — the operator console (RunPackageOut) keeps those.
    """

    run_id: str
    narrative_slug: str
    title: str | None = None
    lede: str | None = None
    video: RunArtifactOut | None = None
    documentation: RunArtifactOut | None = None
    narrative: RunNarrativeOut | None = None
    # Live systems the run used, shown as named buttons.
    product_links: list[WalkthroughLink] = []
    related_links: list[WalkthroughLink] = []
    is_public: bool = False
    is_member: bool = False
    share_token: str | None = None
    # Absolute, token-bearing `/w/<ws>/ddd-release/<narrative>/<run>?t=…` —
    # only when the release is public.
    share_url: str | None = None
    build_url: str | None = None


class NarrativeVisibilityIn(StrictModel):
    visibility: Literal["private", "link"]


class NarrativeVisibilityOut(StrictModel):
    slug: str
    visibility: Literal["public", "private", "mixed"]
    walkthroughs_updated: int
    reviews_updated: int


class NarrativeMoveIn(StrictModel):
    """Move a narrative to another workspace.

    ``dry_run`` defaults to True on purpose: the plan is the only record of
    where the artifacts were, and the move has no undo beyond running it in
    reverse. Read the plan, then repeat with ``dry_run: false``.
    """

    to_workspace: str
    dry_run: bool = True
    also: list[str] = []
    """Extra narrative slugs to move in the SAME transaction — a set of
    narratives that belong together should not land half-moved."""


class NarrativeMoveNarrativeOut(StrictModel):
    versions_by_workspace: dict[str, list[int]]
    walkthroughs: int
    split: bool
    """True when this narrative's versions are spread across more than one
    workspace — the condition that makes its own history unreadable."""


class NarrativeMoveOut(StrictModel):
    target: str
    dry_run: bool
    narratives: dict[str, NarrativeMoveNarrativeOut]
    source_workspaces: list[str]
    reviews_to_move: int
    walkthroughs_to_move: int
    storyboards_to_move: int
