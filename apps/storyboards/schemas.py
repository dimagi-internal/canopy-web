"""Pydantic v2 schemas for /api/storyboards."""
from __future__ import annotations

from typing import Literal

from apps.common.schemas import StrictModel
from apps.feedback.schemas import ArtifactSignalsOut
from apps.walkthroughs.schemas import CutAppLinkOut

Capability = Literal["read", "comment", "suggest"]
Layout = Literal["review", "reel"]


class CutRef(StrictModel):
    """One cut of one narrative on the board — what a short version lists."""

    narrative_slug: str
    cut_id: str = ""
    """Blank = the whole narrative."""


class ShortVersion(StrictModel):
    """A subset of the arc for when there is less time ("the 3-minute version")."""

    label: str = ""
    note: str = ""
    items: list[CutRef] = []


class Divergence(StrictModel):
    """A place where what the script says and what the data shows disagree."""

    script: str
    data: str = ""
    resolution: str = ""
    narrative_slug: str = ""
    """Which narrative it is about — set on the way out for a narrative's own
    divergences; optional on a board-level one."""


class StoryboardBrief(StrictModel):
    """Optional framing for the whole set. Every field may be absent."""

    audience: str = ""
    """Who the demos are for — "Edesia and OES"."""
    purpose: str = ""
    """The one point the set must land."""
    data_disclosure: str = ""
    """What the reader must know about the data on screen — "All on-screen data
    is synthetic; source: Labs programme 10112"."""
    short_version: ShortVersion | None = None
    assumptions: list[str] = []
    divergences: list[Divergence] = []
    open_questions: list[str] = []


class CutSceneOut(StrictModel):
    id: str
    title: str
    text: str


class EntryCutOut(StrictModel):
    """One recorded cut of an entry's current version, with what a reviewer
    needs to judge it without a companion doc. Built from the cut's uploaded
    video plus the optional ``cuts[]`` the publisher sent in the version's
    request_json (``apps.storyboards.cuts``)."""

    cut_id: str
    anchor_id: str
    """What a note on this cut anchors to: ``cut:<cut_id>``."""
    title: str
    recorded: bool
    """False for a cut the publisher declared but no video is pinned to yet."""
    video_url: str | None = None
    video_viewer_url: str | None = None
    poster_url: str | None = None
    duration_sec: int | None = None
    word_count: int = 0
    scenes: list[CutSceneOut] = []
    screen_notes: str = ""
    """What is on screen and where the cursor goes."""
    audience_note: str = ""
    """Why this cut matters to the board's audience."""
    provenance: str = ""
    """Where the on-screen data comes from."""
    app_links: list[CutAppLinkOut] = []
    """The live product screens this cut films, for the reviewer to open."""
    optional: bool = False


class EntryOut(StrictModel):
    narrative_slug: str
    title: str
    lede: str
    version: int | None
    video_url: str | None
    video_viewer_url: str | None
    published: bool
    cuts: list[EntryCutOut] = []
    """A recorded narrative's cuts, in running order. Empty for an explainer."""
    assumptions: list[str] = []
    divergences: list[Divergence] = []
    open_questions: list[str] = []
    """The narrative's own caveats, from its version's request_json."""


class ActOut(StrictModel):
    anchor_id: str
    """What an act-level note anchors to. Resolvable back to the act via this
    same read — feedback carries a pointer, never a copy of the act."""
    title: str
    prose: str
    entries: list[EntryOut]


class DecisionIn(StrictModel):
    """One decision row as a publisher sends it (spec B). Person-owned fields —
    decided_by, confirmed, history — are not accepted: the server sets them."""

    id: str
    scope: str = ""
    question: str
    plain_question: str = ""
    options: list[str] = []
    recommended: str = ""
    override: str = ""
    reasoning: str = ""
    source: str = ""
    evidence_basis: Literal["stated", "inferred", "conflicting"] = "inferred"
    conflict_signals: list[str] = []
    status: Literal["ai-default", "deferred"] = "ai-default"
    revisit_when: str = ""
    review_ask: Literal["", "recommended-confirmation", "required-before"] = ""
    needed_by: Literal["", "external-share", "next-run"] = ""
    confirm_reason: str = ""
    supersedes: str = ""


class DecisionHistoryOut(StrictModel):
    status: str
    value: str
    decided_by: str
    at: str


class StagedEditOut(StrictModel):
    edit_id: str
    key: str
    action: str
    value: str
    reasoning: str
    by_name: str
    at: str


class DecisionOut(StrictModel):
    id: str
    scope: str
    question: str
    plain_question: str
    options: list[str]
    recommended: str
    override: str
    value: str
    reasoning: str
    override_reasoning: str
    source: str
    evidence_basis: str
    conflict_signals: list[str]
    status: str
    revisit_when: str
    review_ask: str
    needed_by: str
    confirm_reason: str
    decided_by: str
    decided_by_verified: bool
    decided_at: str | None
    confirmed: bool
    history: list[DecisionHistoryOut]
    supersedes: str
    superseded_by: str
    editable: bool
    """False for a narrative's read-time caveat: shown, not yet a stored row."""
    pending: list[StagedEditOut] = []
    """Edits staged in the open round, applied when it closes."""


class DecisionEditIn(StrictModel):
    action: Literal["confirm", "pick", "write_in", "undo"]
    value: str = ""
    reasoning: str = ""
    author_name: str = ""
    """A guest's name (plan C, inside a round). Ignored for a signed-in member."""


class RoundCloseIn(StrictModel):
    close_note: str = ""
    picks: dict[str, str] = {}
    """`picks` maps a decision id to the staged edit chosen where people disagreed."""


class RoundOut(StrictModel):
    number: int
    state: str
    opened_at: str
    opened_by: str
    closed_at: str | None
    close_note: str
    pinned_versions: dict[str, int]
    note_count: int
    people_count: int


class RoundNoteOut(StrictModel):
    id: int
    kind: str
    body: str
    suggested_text: str
    author_name: str
    anchor_id: str
    target_kind: str
    target_ref: str
    target_version: int | None
    state: str
    disposition_note: str
    resolved_in_version: int | None
    created_at: str
    mine: bool


class RoundPayloadOut(RoundOut):
    notes: list[RoundNoteOut] = []
    staged_edits: list[StagedEditOut] = []


class RoundDetailNoteOut(RoundNoteOut):
    author_email: str


class RoundDetailOut(RoundOut):
    notes: list[RoundDetailNoteOut]
    staged_edits: list[StagedEditOut]
    decisions: list[DecisionOut]
    board_url: str


class RoundConsumeIn(StrictModel):
    run_id: str


class StoryboardOut(StrictModel):
    slug: str
    title: str
    lede: str
    brief: StoryboardBrief = StoryboardBrief()
    decisions: list[DecisionOut] = []
    capability: str
    layout: str
    is_member: bool = False
    """True when the caller belongs to the owning workspace — i.e. is one of the
    people who SENT this link, not one of the people it was sent to. The only
    thing it changes is whether returning notes are shown."""
    acts: list[ActOut]
    # Round notes are visible to every reader while the round is open (spec C,
    # Visibility) — names only, never emails.
    round: RoundPayloadOut | None = None
    previous_round: RoundPayloadOut | None = None


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


class StoryboardListItemOut(ArtifactSignalsOut):
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
    brief: StoryboardBrief | None = None
    decisions: list[DecisionIn] | None = None
    capability: Capability = "read"
    layout: Layout = "review"
    acts: list[ActIn] = []
    # What made it (T76) — optional overrides of the caller's X-Canopy-Parent-*
    # headers and of the parent turn's board-task project.
    session_id: str | None = None
    turn_id: str | None = None
    agent_project: str | None = None


class StoryboardPatchIn(StrictModel):
    """Everything optional — a retitle and a reorder are separate gestures."""

    title: str | None = None
    lede: str | None = None
    brief: StoryboardBrief | None = None
    """Replaces the whole brief (send ``{}`` to clear it)."""
    # Replaces the whole set; a ruled row may be reworded, never moved or dropped.
    decisions: list[DecisionIn] | None = None
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
