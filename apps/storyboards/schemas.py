"""Pydantic v2 schemas for /api/storyboards."""
from __future__ import annotations

from typing import Literal

from apps.common.schemas import StrictModel

Capability = Literal["read", "comment", "suggest"]


class EntryOut(StrictModel):
    narrative_slug: str
    title: str
    lede: str
    version: int | None
    video_url: str | None
    video_viewer_url: str | None
    published: bool


class ActOut(StrictModel):
    anchor_id: str
    """What an act-level note anchors to. Resolvable back to the act via this
    same read — feedback carries a pointer, never a copy of the act."""
    title: str
    prose: str
    entries: list[EntryOut]


class StoryboardOut(StrictModel):
    slug: str
    title: str
    lede: str
    capability: str
    acts: list[ActOut]


class StoryboardListItemOut(StrictModel):
    slug: str
    title: str
    lede: str
    capability: str
    act_count: int
    share_url: str | None
    """Absolute, token-bearing link. Members only — this is the thing you send."""


class StoryboardListOut(StrictModel):
    items: list[StoryboardListItemOut]


class EntryIn(StrictModel):
    narrative_slug: str
    pinned_run_id: str = ""


class ActIn(StrictModel):
    title: str
    prose: str = ""
    entries: list[EntryIn] = []


class StoryboardIn(StrictModel):
    slug: str
    title: str
    lede: str = ""
    capability: Capability = "read"
    acts: list[ActIn] = []


class StoryboardPatchIn(StrictModel):
    """Everything optional — a retitle and a reorder are separate gestures."""

    title: str | None = None
    lede: str | None = None
    capability: Capability | None = None
    acts: list[ActIn] | None = None


class ShareTokenOut(StrictModel):
    share_url: str


class AnonFeedbackIn(StrictModel):
    """An outsider's comment or suggestion, arriving with a share token.

    Deliberately NOT the same shape as `feedback.FeedbackIn`: an anonymous
    caller may not choose its own `channel`, `target_kind` or `source_ref`, and
    must not be able to file feedback against a target the board does not
    contain. The server fills those in.
    """

    narrative_slug: str = ""
    """Blank = feedback on the whole storyboard."""
    target_version: int | None = None
    anchor_id: str = ""
    kind: Literal["comment", "suggestion"] = "comment"
    body: str = ""
    suggested_text: str = ""
    author_name: str = ""
    author_email: str = ""


class NarrationItemOut(StrictModel):
    id: str
    title: str
    text: str


class NarrativeReadOut(StrictModel):
    narrative_slug: str
    title: str
    story: str
    version: int | None
    previous_version: int | None
    narration: list[NarrationItemOut]
    previous_narration: list[NarrationItemOut]
    storyboard_slug: str
    storyboard_title: str
    capability: str
