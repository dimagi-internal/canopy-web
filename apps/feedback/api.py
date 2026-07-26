"""Django Ninja router for /api/feedback — ingest, read, resolve.

Deliberately thin over ``services`` so a later MCP tool shares one
implementation.

canopy-web is not an integration hub: it owns what happens IN canopy-web. Email
and Google-Doc feedback arrive because an AGENT reads them and POSTs here with
its PAT — there is no poller, no third-party credential, and no inbound
connector in this app. That is exactly what lets it stay generic over
``channel`` instead of growing one integration per source.

Auth is session-or-PAT (``session_auth`` resolves both, since
``BearerTokenAuthMiddleware`` sets ``request.user`` upstream). The anonymous
share-token submit arrives with L3 — the token that would gate it is minted by
the storyboard, which does not exist yet.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.feedback import services
from apps.feedback.models import Feedback
from apps.feedback.schemas import (
    FeedbackBatchIn,
    FeedbackIngestOut,
    FeedbackListOut,
    FeedbackOut,
    FeedbackResolveIn,
)

router = Router(auth=session_auth, tags=["feedback"])


def _out(fb: Feedback) -> dict:
    return {
        "id": fb.pk,
        "target_kind": fb.target_kind,
        "target_ref": fb.target_ref,
        "target_version": fb.target_version,
        "anchor_id": fb.anchor_id,
        "kind": fb.kind,
        "body": fb.body,
        "suggested_text": fb.suggested_text,
        "author_name": fb.author_name,
        "author_email": fb.author_email,
        "channel": fb.channel,
        "source_ref": fb.source_ref,
        "state": fb.state,
        "disposition_note": fb.disposition_note,
        "resolved_in_version": fb.resolved_in_version,
        "created_at": fb.created_at.isoformat(),
    }


@router.post("/", response=FeedbackIngestOut, summary="Ingest feedback (batch, idempotent)")
def ingest_feedback(request: HttpRequest, payload: FeedbackBatchIn) -> dict:
    """Idempotent per ``(channel, source_ref)`` so re-reading a mailbox or a doc
    is safe. ``submitted_by`` is the CALLER (the agent's PAT user, or the logged
    in human) — never the external author, who has no account here."""
    return services.ingest(
        [item.model_dump() for item in payload.items],
        submitted_by=request.user if request.user.is_authenticated else None,
    )


@router.get("/", response=FeedbackListOut, summary="List feedback")
def list_feedback(
    request: HttpRequest,
    target_kind: str | None = None,
    target_ref: str | None = None,
    state: str | None = None,
    channel: str | None = None,
) -> dict:
    qs = services.list_feedback(
        target_kind=target_kind, target_ref=target_ref, state=state, channel=channel
    )
    return {"items": [_out(fb) for fb in qs]}


@router.post("/{feedback_id}/resolve", response=FeedbackOut, summary="Record a disposition")
def resolve_feedback(request: HttpRequest, feedback_id: int, payload: FeedbackResolveIn) -> dict:
    """How a decision turn records what it did. The only mutation — feedback is
    what somebody said, and editing that after the fact would make the pool
    untrustworthy as a record."""
    try:
        fb = services.resolve(
            feedback_id,
            state=payload.state,
            note=payload.note,
            resolved_in_version=payload.resolved_in_version,
        )
    except Feedback.DoesNotExist:
        raise HttpError(404, "feedback not found")
    return _out(fb)
