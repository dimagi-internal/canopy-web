"""Pydantic schemas for the /api/reviews/ surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from apps.common.schemas import StrictModel

ReviewStatus = Literal["pending", "resolved"]
ReviewVisibility = Literal["private", "link"]


class ReviewRequestOut(StrictModel):
    """Detail/list output for a review request."""

    id: uuid.UUID
    run_id: str
    # Narrative slug this review belongs to (explicit narrative_slug, else derived from
    # run_id) — lets the DDD shell highlight the right narrative on the editor.
    # None for a run-child gate, which belongs to no narrative: the DDD shell is not
    # its chrome and highlighting one would be a lie.
    narrative_slug: str | None = None
    gate: str
    status: ReviewStatus
    visibility: ReviewVisibility
    request_json: dict[str, Any]
    response_json: dict[str, Any] | None = None
    # Suggestions from external (share-token) reviewers who cannot resolve the gate.
    # Only populated for callers who can write (the owner / a workspace member);
    # empty for anonymous link readers so one external reviewer can't see another's.
    suggestions: list[dict[str, Any]] = []
    is_owner: bool
    # Whether THIS caller may resolve the gate (an editor of the review's workspace).
    # The page decides between the decide-and-submit editor and the suggest-only
    # editor on this — not on "is anyone signed in", which handed a signed-in
    # non-editor an approve button the server then refused (canopy-web#1268).
    can_decide: bool = False
    # The narrative's human name (apps.reviews.titles) — the review page's subtitle
    # was a run id like "chlorine-dispenser-walkthroughs-2026-10-07-002" (#1271).
    title: str | None = None
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None


class ReviewListItemOut(StrictModel):
    """One row in the DDD-plans dashboard list (GET /api/reviews/)."""

    id: uuid.UUID
    run_id: str
    gate: str
    status: ReviewStatus
    visibility: ReviewVisibility
    # Derived from request_json for a scannable list — never the raw payload.
    # None for a run-child gate (see ReviewRequestOut.narrative_slug).
    narrative_slug: str | None = None
    title: str | None = None
    scene_count: int = 0
    created_at: dt.datetime
    resolved_at: dt.datetime | None = None
    # resolved_at when resolved, else created_at — the "last edit" the dashboard sorts by.
    last_activity_at: dt.datetime
    is_owner: bool


class ReviewCreateIn(StrictModel):
    """Body of POST /api/reviews/: the inbound request_json plus optional meta."""

    # The full request_json payload from the canopy orchestrator.
    request_json: dict[str, Any]
    # Optional: link visibility so the review page is publicly shareable.
    visibility: ReviewVisibility = "link"


class ReviewSubmitIn(StrictModel):
    """Body of POST /api/reviews/<id>/submit/: the human's response."""

    response_json: dict[str, Any]


class ReviewSuggestIn(StrictModel):
    """Body of POST /api/reviews/<id>/suggest/: suggested edits from an external
    (share-token) reviewer, or from a workspace member saving edits without
    deciding. Same response_json shape as a submit, but it is stored as a
    SUGGESTION — it never resolves the gate. The review's owner is notified."""

    response_json: dict[str, Any]
    name: str | None = None


class ReviewSuggestOut(StrictModel):
    """Slim ack for a suggestion — the suggester does not get to see others'
    suggestions, only that theirs landed."""

    ok: bool
    suggestion_count: int


class ReviewCreateOut(StrictModel):
    """Slim response from POST /api/reviews/: just enough for the orchestrator to poll."""

    id: uuid.UUID
    url: str
    # For a link-visibility review: the per-review share token that lets an external
    # (non-dimagi) reviewer submit SUGGESTIONS via ?t=<token>. None for private reviews.
    share_token: str | None = None
