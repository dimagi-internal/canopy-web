"""Django Ninja router for /api/storyboards.

The public read follows ``apps/runs/api.py::get_run_release`` exactly: ``auth=None``
on the route, the handler self-enforces (workspace member OR a matching
``?t=<share_token>``), and the login middleware allowlists the path. A missing or
wrong token **404s, never 403s** — a 403 would confirm the board exists, which is
the existence leak walkthroughs already avoid.

The anonymous FEEDBACK route lives here, not in ``apps/feedback``, and that is
deliberate. ``feedback`` is framework tier; ``storyboards`` is product. Having the
framework app resolve a storyboard token would invert the one-way rule. The
storyboard owns the token, so it owns the route that accepts a token-bearing
write — and calls ``feedback.services.ingest``, which is exactly why that service
layer is request-free.
"""
from __future__ import annotations

from django.db import transaction
from django.http import HttpRequest
from django.urls import get_script_prefix
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.feedback import services as feedback_services
from apps.storyboards import services
from apps.storyboards.act_keys import act_key
from apps.storyboards.models import Act, Entry, Storyboard
from apps.storyboards.schemas import (
    AnonFeedbackIn,
    NotesOut,
    ShareTokenOut,
    StoryboardIn,
    StoryboardListOut,
    StoryboardOut,
    NarrativeReadOut,
    StoryboardPatchIn,
)
from apps.workspaces import services as wsvc

router = Router(tags=["storyboards"])


# ---------------------------------------------------------------- access rules


def _member_boards(request: HttpRequest):
    """Boards in workspaces the caller belongs to. Empty for anonymous."""
    if not request.user.is_authenticated:
        return Storyboard.objects.none()
    return Storyboard.objects.filter(workspace_id__in=wsvc.user_workspace_slugs(request.user))


def _is_member(request: HttpRequest, board: Storyboard) -> bool:
    """One of the people who SENT the link, rather than one it was sent to."""
    return bool(
        request.user.is_authenticated
        and board.workspace_id in wsvc.user_workspace_slugs(request.user)
    )


def _readable_or_404(request: HttpRequest, slug: str) -> Storyboard:
    """A board the caller may READ, or 404.

    404 rather than 403 on a bad token is the point: the response for "no such
    board" and "wrong token" must be indistinguishable.
    """
    board = Storyboard.objects.filter(slug=slug).first()
    if board is None:
        raise HttpError(404, "storyboard not found")

    if request.user.is_authenticated:
        wsvc.auto_join_workspaces(request.user)
        if board.workspace_id in wsvc.user_workspace_slugs(request.user):
            return board

    if board.token_matches(request.GET.get("t")):
        return board

    raise HttpError(404, "storyboard not found")


def _owned_or_404(request: HttpRequest, slug: str) -> Storyboard:
    board = _member_boards(request).filter(slug=slug).first()
    if board is None:
        raise HttpError(404, "storyboard not found")
    return board


def _share_url(request: HttpRequest, board: Storyboard) -> str | None:
    """The absolute, token-bearing link — the thing you actually send someone.

    Must include the deployment's script prefix: labs serves under
    ``FORCE_SCRIPT_NAME=/canopy``, and ``build_absolute_uri`` on a leading-slash
    path drops it, minting a link that 404s. Same fix apps/walkthroughs already
    carries; reuse it rather than rediscovering it (this one shipped broken and
    was caught by opening the link).
    """
    if not board.share_token:
        return None
    prefix = get_script_prefix().rstrip("/")  # "" locally, "/canopy" on labs
    return request.build_absolute_uri(
        f"{prefix}/storyboard/{board.slug}?t={board.share_token}"
    )


# ------------------------------------------------------------------- write ops


def _replace_acts(board: Storyboard, acts) -> None:
    """Wholesale replace — reordering is a rewrite, not a diff.

    Same call the import command makes, so authoring from a repo and editing in
    the UI cannot drift apart.
    """
    board.acts.all().delete()
    taken: set[str] = set()
    for a_pos, act_in in enumerate(acts):
        act = Act.objects.create(
            storyboard=board,
            key=act_key(act_in.key, act_in.title, a_pos, taken),
            title=act_in.title,
            prose=act_in.prose,
            position=a_pos,
        )
        for e_pos, entry_in in enumerate(act_in.entries):
            Entry.objects.create(
                act=act,
                narrative_slug=entry_in.narrative_slug,
                pinned_run_id=entry_in.pinned_run_id,
                position=e_pos,
            )


# --------------------------------------------------------------------- routes


@router.get("/", response=StoryboardListOut, auth=session_auth, summary="List storyboards")
def list_storyboards(request: HttpRequest) -> dict:
    boards = _member_boards(request).prefetch_related("acts")
    return {
        "items": [
            {
                "slug": b.slug,
                "title": b.title,
                "lede": b.lede,
                "capability": b.capability,
                "act_count": b.acts.count(),
                "share_url": _share_url(request, b),
            }
            for b in boards
        ]
    }


@router.post("/", response=StoryboardOut, auth=session_auth, summary="Create a storyboard")
def create_storyboard(request: HttpRequest, payload: StoryboardIn) -> dict:
    workspace_slug = getattr(request, "workspace_slug", None)
    if not workspace_slug:
        ws = wsvc.user_default_workspace(request.user)
        if ws is None:
            raise HttpError(400, "no workspace to create this storyboard in")
        workspace_slug = ws.slug
    with transaction.atomic():
        board = Storyboard.objects.create(
            slug=payload.slug,
            title=payload.title,
            lede=payload.lede,
            capability=payload.capability,
            workspace_id=workspace_slug,
        )
        _replace_acts(board, payload.acts)
    return services.resolve_board(board, is_member=True)


@router.get(
    "/{slug}",
    response=StoryboardOut,
    auth=None,
    summary="Read a storyboard (public via ?t=<share_token>)",
)
def get_storyboard(request: HttpRequest, slug: str) -> dict:
    """Anonymous-capable; the handler self-enforces. See the module docstring."""
    board = _readable_or_404(request, slug)
    return services.resolve_board(board, is_member=_is_member(request, board))


@router.patch("/{slug}", response=StoryboardOut, auth=session_auth, summary="Edit a storyboard")
def patch_storyboard(request: HttpRequest, slug: str, payload: StoryboardPatchIn) -> dict:
    board = _owned_or_404(request, slug)
    with transaction.atomic():
        fields = []
        for name in ("title", "lede", "capability"):
            value = getattr(payload, name)
            if value is not None:
                setattr(board, name, value)
                fields.append(name)
        if fields:
            board.save(update_fields=[*fields, "updated_at"])
        if payload.acts is not None:
            _replace_acts(board, payload.acts)
    return services.resolve_board(board, is_member=True)


@router.post(
    "/{slug}/rotate-token",
    response=ShareTokenOut,
    auth=session_auth,
    summary="Re-mint the share link, killing every link already sent",
)
def rotate_token(request: HttpRequest, slug: str) -> dict:
    board = _owned_or_404(request, slug)
    board.rotate_share_token()
    return {"share_url": _share_url(request, board)}


@router.post(
    "/{slug}/share",
    response=ShareTokenOut,
    auth=session_auth,
    summary="Mint the share link if it does not exist yet",
)
def ensure_token(request: HttpRequest, slug: str) -> dict:
    board = _owned_or_404(request, slug)
    board.ensure_share_token()
    return {"share_url": _share_url(request, board)}


@router.post(
    "/{slug}/feedback",
    response={200: dict},
    auth=None,
    summary="Leave feedback on a storyboard (public via ?t=<share_token>)",
)
def leave_feedback(request: HttpRequest, slug: str, payload: AnonFeedbackIn) -> dict:
    """The anonymous write L2 deferred, gated by the board's capability.

    A ``suggestion`` needs the ``suggest`` grant; a ``comment`` needs
    ``comment``. A read-only link can do neither. The caller cannot choose its
    own channel or target kind — the server fills those in, so an outsider
    cannot file feedback against something this board does not contain.
    """
    board = _readable_or_404(request, slug)
    token = request.GET.get("t")

    needed = Storyboard.CAP_SUGGEST if payload.kind == "suggestion" else Storyboard.CAP_COMMENT
    is_member = request.user.is_authenticated and board.workspace_id in wsvc.user_workspace_slugs(
        request.user
    )
    if not is_member and not board.grants(needed, token):
        raise HttpError(403, f"this link does not grant {needed}")

    if payload.narrative_slug:
        known = Entry.objects.filter(
            act__storyboard=board, narrative_slug=payload.narrative_slug
        ).exists()
        if not known:
            raise HttpError(404, "no such narrative on this storyboard")

    if not payload.body.strip() and not payload.suggested_text.strip():
        raise HttpError(422, "a note needs some words")

    item = {
        "target_kind": "narrative" if payload.narrative_slug else "storyboard",
        "target_ref": payload.narrative_slug or board.slug,
        "target_version": payload.target_version,
        "anchor_id": payload.anchor_id,
        "kind": payload.kind,
        "body": payload.body,
        "suggested_text": payload.suggested_text,
        "author_name": payload.author_name,
        "author_email": payload.author_email,
        "channel": "web",
        "source_ref": "",
    }
    return feedback_services.ingest(
        [item],
        submitted_by=request.user if request.user.is_authenticated else None,
    )


@router.get(
    "/{slug}/notes",
    response=NotesOut,
    auth=session_auth,
    summary="Read the notes left on this board (members only)",
)
def list_notes(request: HttpRequest, slug: str) -> dict:
    """What came back. Members only, and 404 — never 403 — for everyone else.

    Without this the loop had no closing half: a reviewer could leave notes and
    the people who sent them the link had no way to read them short of curl.

    A token holder must not reach it. Seeing the other reviewers' notes would
    bias exactly the independent read you asked for, and the rows carry names
    that were given to us rather than to each other — so this uses
    ``_owned_or_404`` (membership), not ``_readable_or_404`` (membership OR
    token).
    """
    board = _owned_or_404(request, slug)
    return {
        "items": [
            {
                "id": fb.pk,
                "kind": fb.kind,
                "body": fb.body,
                "suggested_text": fb.suggested_text,
                "author_name": fb.author_name,
                "channel": fb.channel,
                "state": fb.state,
                "target_kind": fb.target_kind,
                "target_ref": fb.target_ref,
                "target_version": fb.target_version,
                "anchor_id": fb.anchor_id,
                "created_at": fb.created_at.isoformat(),
            }
            for fb in services.board_feedback(board)
        ]
    }


@router.get(
    "/{slug}/narratives/{narrative_slug}",
    response=NarrativeReadOut,
    auth=None,
    summary="Read one narrative on this storyboard (public via ?t=<share_token>)",
)
def get_board_narrative(request: HttpRequest, slug: str, narrative_slug: str) -> dict:
    """The reviewer surface's read. Gated by the SAME token as the board.

    404s when the narrative is not on this board — a link to one arc must not be
    a read handle for every narrative in the workspace.
    """
    board = _readable_or_404(request, slug)
    data = services.resolve_narrative(board, narrative_slug)
    if data is None:
        raise HttpError(404, "no such narrative on this storyboard")
    return data
