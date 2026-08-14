"""Django Ninja routers for shared Claude Code session transcripts.

Two routers:
  * ``router`` (auth=session_auth) → /api/sessions  — owner upload + management
  * ``share_router`` (auth=None)   → /api/share     — public read-only view

The public read route is auth=None AND exempted in
``apps.common.middleware.LoginRequiredMiddleware`` (see ``_is_share_link``) so
an anonymous visitor with a valid token can load it without a dimagi session.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.db import models, transaction
from django.http import HttpRequest
from django.utils.dateparse import parse_datetime
from ninja import File, Form, Router, Status
from ninja.files import UploadedFile

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, ProblemError

from . import redact
from .models import (
    ArcShareToken,
    Message,
    Session,
    SessionArc,
    SessionArcItem,
    ShareToken,
)
from .parser import parse_session_file
from .schemas import (
    ArcCreateIn,
    ArcCreateOut,
    ArcDetailOut,
    ArcListItemOut,
    ArcPatchIn,
    SessionDetailOut,
    SessionListItemOut,
    SessionMessageOut,
    SessionPatchIn,
    SessionRotateTokenOut,
    SessionUploadOut,
    SessionVisibility,
    SharedSectionOut,
    SharedViewOut,
)

router = Router(auth=session_auth, tags=["sessions"])
share_router = Router(tags=["share"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB — transcripts are JSON, rarely large.


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _message_payloads(session: Session) -> list[dict]:
    return [
        {
            "turn_index": m.turn_index,
            "role": m.role,
            "content": m.content,
            "plaintext": m.plaintext,
        }
        for m in session.messages.all()
    ]


def _list_payload(session: Session, *, is_owner: bool) -> dict:
    # A non-owner can only be seeing this row because it's link-visible, and
    # the link IS the read path — so the token ships to them too.
    token = (
        session.active_token()
        if is_owner or session.visibility == Session.VISIBILITY_LINK
        else None
    )
    return {
        "slug": session.slug,
        "title": session.title,
        "project_slug": session.project_slug,
        "visibility": session.visibility,
        "owner_email": session.owner.email,
        "message_count": session.messages.count(),
        "redaction_count": session.redaction_count,
        "share_token": token.token if token else None,
        "is_owner": is_owner,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _get_owned_or_403(request: HttpRequest, slug: str) -> Session:
    session = Session.objects.filter(slug=slug).select_related("owner").first()
    if session is None:
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    if not (request.user.is_authenticated and session.owner_id == request.user.id):
        raise ProblemError(403, "Forbidden — owner only", type_=TYPE_FORBIDDEN)
    return session


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post(
    "/upload",
    response={201: SessionUploadOut},
    summary="Upload a Claude .jsonl transcript (multipart)",
)
def upload_session(
    request: HttpRequest,
    file: UploadedFile = File(...),
    title: str = Form(""),
    project_slug: str = Form(""),
    visibility: SessionVisibility = Form("link"),
    started_at: str = Form(""),
    ended_at: str = Form(""),
    active_seconds: int = Form(0),
) -> Status:
    if file.size > MAX_UPLOAD_BYTES:
        raise ProblemError(
            413,
            "Payload too large",
            detail=f"Transcript exceeds {MAX_UPLOAD_BYTES} bytes.",
        )

    # When the session actually ran + how long it actively took (the uploader
    # reads these from the raw transcript; the reduced upload has no timestamps).
    started_dt = parse_datetime(started_at) if started_at else None
    ended_dt = parse_datetime(ended_at) if ended_at else None
    active_secs = active_seconds if active_seconds > 0 else None

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        for chunk in file.chunks():
            tmp.write(chunk)
        tmp_path = Path(tmp.name)
    try:
        parsed = parse_session_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    # Idempotent re-upload: if this owner already shared this CLI session,
    # return the existing link rather than creating a duplicate.
    if parsed.cli_session_id:
        existing = Session.objects.filter(
            owner=request.user, cli_session_id=parsed.cli_session_id
        ).first()
        if existing is not None:
            # Backfill timing on re-upload if we have it and the stored row doesn't.
            backfill = []
            if started_dt and existing.started_at is None:
                existing.started_at = started_dt
                backfill.append("started_at")
            if ended_dt and existing.ended_at is None:
                existing.ended_at = ended_dt
                backfill.append("ended_at")
            if active_secs and existing.active_seconds is None:
                existing.active_seconds = active_secs
                backfill.append("active_seconds")
            if backfill:
                existing.save(update_fields=[*backfill, "updated_at"])
            token = (
                existing.ensure_share_token(request.user)
                if existing.visibility == Session.VISIBILITY_LINK
                else existing.active_token()
            )
            return Status(
                201,
                SessionUploadOut(
                    slug=existing.slug,
                    message_count=existing.messages.count(),
                    redaction_count=existing.redaction_count,
                    visibility=existing.visibility,
                    share_token=token.token if token else None,
                    duplicate=True,
                ),
            )

    resolved_title = (title.strip() or file.name or "Claude session")[:500]
    resolved_project = project_slug.strip() or None

    with transaction.atomic():
        session = Session.objects.create(
            owner=request.user,
            title=resolved_title,
            project_slug=resolved_project,
            visibility=visibility,
            cli_session_id=parsed.cli_session_id or "",
            source_filename=(file.name or "")[:500],
            raw_bytes=parsed.raw_bytes,
            line_count=parsed.line_count,
            started_at=started_dt,
            ended_at=ended_dt,
            active_seconds=active_secs,
        )

        total_redactions = 0
        rows: list[Message] = []
        for idx, turn in enumerate(parsed.turns, start=1):
            plaintext, content, n = redact.redact_turn(turn.plaintext, turn.content)
            total_redactions += n
            rows.append(
                Message(
                    session=session,
                    turn_index=idx,
                    role=turn.role,
                    content=content,
                    plaintext=plaintext,
                )
            )
        Message.objects.bulk_create(rows)

        session.redaction_count = total_redactions
        session.save(update_fields=["redaction_count", "updated_at"])

        token = None
        if visibility == Session.VISIBILITY_LINK:
            token = ShareToken.objects.create(session=session, created_by=request.user)

    return Status(
        201,
        SessionUploadOut(
            slug=session.slug,
            message_count=len(rows),
            redaction_count=total_redactions,
            visibility=session.visibility,
            share_token=token.token if token else None,
            duplicate=False,
        ),
    )


# ---------------------------------------------------------------------------
# List — mine (any visibility) + everyone's link-shared.
# Agents upload under their OWN accounts (the canopy uploader resolves the PAT
# from the repo it runs in), so an owner-only list hides exactly the shares a
# human asked their agent to make. Link visibility already means "anyone with
# the URL"; surfacing those rows to authed teammates grants nothing new.
# ---------------------------------------------------------------------------


@router.get("/", response=list[SessionListItemOut], summary="List shared sessions")
def list_sessions(
    request: HttpRequest, project: str = ""
) -> list[SessionListItemOut]:
    qs = (
        Session.objects.select_related("owner")
        .filter(
            models.Q(owner=request.user)
            | models.Q(visibility=Session.VISIBILITY_LINK)
        )
        .prefetch_related("share_tokens")
    )
    if project:
        qs = qs.filter(project_slug=project)
    return [
        SessionListItemOut.model_validate(
            _list_payload(s, is_owner=s.owner_id == request.user.id)
        )
        for s in qs
    ]


# ---------------------------------------------------------------------------
# Arcs — ordered groups of owned sessions, shared as one page.
# Registered BEFORE the session `/{slug}` routes so `/arcs` isn't captured as a
# session slug.
# ---------------------------------------------------------------------------


def _arc_list_payload(arc: SessionArc, *, is_owner: bool) -> dict:
    token = (
        arc.active_token()
        if is_owner or arc.visibility == SessionArc.VISIBILITY_LINK
        else None
    )
    return {
        "slug": arc.slug,
        "title": arc.title,
        "project_slug": arc.project_slug,
        "visibility": arc.visibility,
        "owner_email": arc.owner.email,
        "item_count": arc.items.count(),
        "share_token": token.token if token else None,
        "is_owner": is_owner,
        "created_at": arc.created_at,
        "updated_at": arc.updated_at,
    }


def _turn_count(session: Session) -> int:
    """Conversation turns = the human prompts (one user message per turn in the
    reduced upload)."""
    return session.messages.filter(role="user").count()


def _arc_item_payloads(arc: SessionArc) -> list[dict]:
    return [
        {
            "position": item.position,
            "heading": item.heading,
            "session_slug": item.session.slug,
            "session_title": item.session.title,
            "message_count": item.session.messages.count(),
            "turn_count": _turn_count(item.session),
            "started_at": item.session.started_at,
            "ended_at": item.session.ended_at,
            "active_seconds": item.session.active_seconds,
        }
        for item in arc.items.select_related("session")
    ]


def _get_owned_arc_or_403(request: HttpRequest, slug: str) -> SessionArc:
    arc = SessionArc.objects.filter(slug=slug).select_related("owner").first()
    if arc is None:
        raise ProblemError(404, "Arc not found", type_=TYPE_NOT_FOUND)
    if not (request.user.is_authenticated and arc.owner_id == request.user.id):
        raise ProblemError(403, "Forbidden — owner only", type_=TYPE_FORBIDDEN)
    return arc


@router.post(
    "/arcs",
    response={201: ArcCreateOut},
    summary="Create an arc from owned sessions (ordered)",
)
def create_arc(request: HttpRequest, payload: ArcCreateIn) -> Status:
    slugs = [it.session_slug for it in payload.items]
    if len(set(slugs)) != len(slugs):
        raise ProblemError(
            422, "Duplicate session in arc", detail="Each session may appear once."
        )
    owned = {
        s.slug: s
        for s in Session.objects.filter(owner=request.user, slug__in=slugs)
    }
    missing = [sl for sl in slugs if sl not in owned]
    if missing:
        raise ProblemError(
            404,
            "Unknown session(s)",
            detail=f"Not found or not yours: {', '.join(missing)}",
            type_=TYPE_NOT_FOUND,
        )

    with transaction.atomic():
        arc = SessionArc.objects.create(
            owner=request.user,
            title=(payload.title or "").strip()[:500],
            project_slug=(payload.project_slug or "").strip() or None,
            visibility=payload.visibility,
        )
        items = [
            SessionArcItem(
                arc=arc,
                session=owned[it.session_slug],
                position=pos,
                heading=(it.heading or "").strip()[:500],
            )
            for pos, it in enumerate(payload.items, start=1)
        ]
        SessionArcItem.objects.bulk_create(items)
        token = None
        if payload.visibility == SessionArc.VISIBILITY_LINK:
            token = ArcShareToken.objects.create(arc=arc, created_by=request.user)

    return Status(
        201,
        ArcCreateOut(
            slug=arc.slug,
            visibility=arc.visibility,
            item_count=len(items),
            share_token=token.token if token else None,
        ),
    )


@router.get("/arcs", response=list[ArcListItemOut], summary="List arcs")
def list_arcs(request: HttpRequest, project: str = "") -> list[ArcListItemOut]:
    # Same rule as list_sessions: mine + everyone's link-shared.
    qs = (
        SessionArc.objects.select_related("owner")
        .filter(
            models.Q(owner=request.user)
            | models.Q(visibility=SessionArc.VISIBILITY_LINK)
        )
        .prefetch_related("share_tokens", "items")
    )
    if project:
        qs = qs.filter(project_slug=project)
    return [
        ArcListItemOut.model_validate(
            _arc_list_payload(a, is_owner=a.owner_id == request.user.id)
        )
        for a in qs
    ]


@router.get("/arcs/{slug}", response=ArcDetailOut, summary="Get one arc (owner)")
def get_arc(request: HttpRequest, slug: str) -> ArcDetailOut:
    arc = _get_owned_arc_or_403(request, slug)
    payload = _arc_list_payload(arc, is_owner=True)
    payload["items"] = _arc_item_payloads(arc)
    return ArcDetailOut.model_validate(payload)


@router.patch("/arcs/{slug}", response=ArcListItemOut, summary="Update an arc (owner)")
def patch_arc(request: HttpRequest, slug: str, payload: ArcPatchIn) -> ArcListItemOut:
    arc = _get_owned_arc_or_403(request, slug)
    updates = payload.model_dump(exclude_unset=True)
    new_visibility = updates.pop("visibility", None)
    for field, value in updates.items():
        setattr(arc, field, value)
    if updates:
        arc.save()
    if new_visibility == SessionArc.VISIBILITY_LINK:
        arc.ensure_share_token(request.user)
    elif new_visibility == SessionArc.VISIBILITY_PRIVATE:
        arc.revoke_sharing()
    arc.refresh_from_db()
    return ArcListItemOut.model_validate(_arc_list_payload(arc, is_owner=True))


@router.delete("/arcs/{slug}", response={204: None}, summary="Delete an arc (owner)")
def delete_arc(request: HttpRequest, slug: str) -> Status:
    arc = _get_owned_arc_or_403(request, slug)
    arc.delete()
    return Status(204, None)


@router.post(
    "/arcs/{slug}/rotate-token",
    response=SessionRotateTokenOut,
    summary="Rotate arc share token (owner)",
)
def rotate_arc_token(request: HttpRequest, slug: str) -> SessionRotateTokenOut:
    arc = _get_owned_arc_or_403(request, slug)
    token = arc.rotate_share_token(request.user)
    return SessionRotateTokenOut(share_token=token.token)


# ---------------------------------------------------------------------------
# Detail (owner)
# ---------------------------------------------------------------------------


@router.get("/{slug}", response=SessionDetailOut, summary="Get one session (owner)")
def get_session(request: HttpRequest, slug: str) -> SessionDetailOut:
    session = _get_owned_or_403(request, slug)
    payload = _list_payload(session, is_owner=True)
    payload["messages"] = _message_payloads(session)
    return SessionDetailOut.model_validate(payload)


# ---------------------------------------------------------------------------
# Patch (owner) — title / project / visibility
# ---------------------------------------------------------------------------


@router.patch("/{slug}", response=SessionListItemOut, summary="Update a session (owner)")
def patch_session(
    request: HttpRequest, slug: str, payload: SessionPatchIn
) -> SessionListItemOut:
    session = _get_owned_or_403(request, slug)
    updates = payload.model_dump(exclude_unset=True)

    new_visibility = updates.pop("visibility", None)
    for field, value in updates.items():
        setattr(session, field, value)
    if updates:
        session.save()

    if new_visibility == Session.VISIBILITY_LINK:
        session.ensure_share_token(request.user)
    elif new_visibility == Session.VISIBILITY_PRIVATE:
        session.revoke_sharing()

    session.refresh_from_db()
    return SessionListItemOut.model_validate(_list_payload(session, is_owner=True))


# ---------------------------------------------------------------------------
# Delete (owner)
# ---------------------------------------------------------------------------


@router.delete("/{slug}", response={204: None}, summary="Delete a session (owner)")
def delete_session(request: HttpRequest, slug: str) -> Status:
    session = _get_owned_or_403(request, slug)
    session.delete()
    return Status(204, None)


# ---------------------------------------------------------------------------
# Rotate token (owner) — invalidate the old share link, mint a fresh one
# ---------------------------------------------------------------------------


@router.post(
    "/{slug}/rotate-token",
    response=SessionRotateTokenOut,
    summary="Rotate share token (owner)",
)
def rotate_token(request: HttpRequest, slug: str) -> SessionRotateTokenOut:
    session = _get_owned_or_403(request, slug)
    token = session.rotate_share_token(request.user)
    return SessionRotateTokenOut(share_token=token.token)


# ---------------------------------------------------------------------------
# Public read-only share view (no auth)
# ---------------------------------------------------------------------------


def _session_messages_out(session: Session) -> list[SessionMessageOut]:
    return [
        SessionMessageOut(
            turn_index=m.turn_index,
            role=m.role,
            content=m.content,
            plaintext=m.plaintext,
        )
        for m in session.messages.all()
    ]


@share_router.get(
    "/{token}",
    auth=None,
    response=SharedViewOut,
    summary="Public read-only view of a shared session or arc",
)
def public_share_view(request: HttpRequest, token: str) -> SharedViewOut:
    # A token is either a single-session token or an arc token. Try session
    # first (the common case); 404 on missing OR revoked — never leak which.
    share = ShareToken.objects.select_related("session").filter(token=token).first()
    if share is not None and share.revoked_at is None:
        session = share.session
        return SharedViewOut(
            kind="session",
            title=session.title,
            redaction_count=session.redaction_count,
            turn_count=_turn_count(session),
            started_at=session.started_at,
            ended_at=session.ended_at,
            active_seconds=session.active_seconds,
            messages=_session_messages_out(session),
        )

    arc_share = (
        ArcShareToken.objects.select_related("arc").filter(token=token).first()
    )
    if arc_share is not None and arc_share.revoked_at is None:
        arc = arc_share.arc
        sections = [
            SharedSectionOut(
                heading=(item.heading or item.session.title),
                redaction_count=item.session.redaction_count,
                turn_count=_turn_count(item.session),
                started_at=item.session.started_at,
                ended_at=item.session.ended_at,
                active_seconds=item.session.active_seconds,
                messages=_session_messages_out(item.session),
            )
            for item in arc.items.select_related("session")
        ]
        starts = [s.started_at for s in sections if s.started_at]
        ends = [s.ended_at for s in sections if s.ended_at]
        active_total = sum(s.active_seconds or 0 for s in sections)
        return SharedViewOut(
            kind="arc",
            title=arc.title,
            redaction_count=sum(s.redaction_count for s in sections),
            turn_count=sum(s.turn_count for s in sections),
            started_at=min(starts) if starts else None,
            ended_at=max(ends) if ends else None,
            active_seconds=active_total or None,
            sections=sections,
        )

    raise ProblemError(404, "Share link not found", type_=TYPE_NOT_FOUND)
