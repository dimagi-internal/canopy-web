"""Pydantic v2 schemas for /api/storyboards."""
from __future__ import annotations

from typing import Literal

from apps.common.schemas import StrictModel

Capability = Literal["read", "comment", "suggest"]
Layout = Literal["review", "reel"]


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
    layout: str
    is_member: bool = False
    """True when the caller belongs to the owning workspace — i.e. is one of the
    people who SENT this link, not one of the people it was sent to. The only
    thing it changes is whether returning notes are shown."""
    acts: list[ActOut]


class NoteOut(StrictModel):
    """One note as its recipients read it. No `author_email`, `submitted_by` or
    `source_ref`: this view answers "what did they say", and the full record is
    `/api/feedback/`."""

    id: int
    kind: str
    body: str
    suggested_text: str
    author_name: str
    channel: str
    state: str
    target_kind: str
    target_ref: str
    target_version: int | None
    anchor_id: str
    created_at: str


class NotesOut(StrictModel):
    items: list[NoteOut]


class StoryboardListItemOut(StrictModel):
    slug: str
    title: str
    lede: str
    capability: str
    layout: str
    act_count: int
    share_url: str | None
    """Absolute, token-bearing link. Members only — this is the thing you send."""


class StoryboardListOut(StrictModel):
    items: list[StoryboardListItemOut]


class EntryIn(StrictModel):
    narrative_slug: str
    pinned_run_id: str = ""
    title: str = ""
    """Blank = derive the card heading from the narrative."""
    blurb: str = ""
    """Blank = derive the one-liner from the narrative's opening sentence."""


class ActIn(StrictModel):
    key: str = ""
    """Optional stable identity. Declare it to keep act notes attached through
    a retitle; otherwise it is derived from the title."""
    title: str
    prose: str = ""
    entries: list[EntryIn] = []


class StoryboardIn(StrictModel):
    slug: str
    title: str
    lede: str = ""
    capability: Capability = "read"
    layout: Layout = "review"
    acts: list[ActIn] = []


class StoryboardPatchIn(StrictModel):
    """Everything optional — a retitle and a reorder are separate gestures."""

    title: str | None = None
    lede: str | None = None
    capability: Capability | None = None
    layout: Layout | None = None
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
