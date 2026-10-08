"""Django Ninja v2 router for the walkthroughs surface."""
from __future__ import annotations

import json
import logging
import re
from uuid import UUID

from django.conf import settings
from django.http import Http404, HttpRequest
from pydantic import ValidationError

log = logging.getLogger(__name__)
from ninja import File, Form, Router, Status
from ninja.files import UploadedFile

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_DRIVE_NOT_CONFIGURED,
    TYPE_DRIVE_UPLOAD_FAILED,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_PAYLOAD_TOO_LARGE,
    TYPE_VALIDATION,
    ProblemError,
)

from apps.runs.ddd import narrative_slug_from_run_id
from apps.runs.aggregate import has_narrative_version
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import storage
from .drive_client import DriveNotConfigured
from .models import Walkthrough
from .schemas import (
    WalkthroughDetailOut,
    WalkthroughKind,
    WalkthroughLink,
    WalkthroughListItemOut,
    WalkthroughPatchIn,
    WalkthroughVisibility,
)

router = Router(auth=session_auth, tags=["walkthroughs"])

CONTENT_TYPE_BY_KIND: dict[str, str] = {"html": "text/html", "video": "video/mp4"}
FILENAME_BY_KIND: dict[str, str] = {"html": "slideshow.html", "video": "video.mp4"}

# Terminal DDD package artifacts — the ones ddd-upload publishes. Uploading one
# without a narrative is what produces a "no narrative" package, so the server
# refuses it as a backstop to the plugin-side guard. Intermediate ddd-run
# artifacts (deck/clip) and non-DDD walkthrough-share uploads carry neither of
# these roles and are unaffected.
_NARRATIVE_REQUIRED_ROLES = {Walkthrough.ROLE_HERO_VIDEO, Walkthrough.ROLE_DOCS}
# A recorded narrative's cut id — the recipe's kebab-case cuts[].id.
_CUT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")


def _require_enabled() -> None:
    if not getattr(settings, "WALKTHROUGHS_ENABLED", True):
        raise Http404("walkthroughs disabled")


def _parse_links_field(raw: str) -> list[dict]:
    """Parse the multipart ``links`` form field (a JSON-encoded list).

    Each entry is validated against :class:`WalkthroughLink`. Returns a list
    of plain dicts ready to store on the JSONField. An empty/blank field
    yields ``[]``. Raises ProblemError(422) on malformed input so a buggy
    uploader fails loud rather than silently dropping the links.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProblemError(422, "Invalid links JSON", detail=str(exc))
    if not isinstance(parsed, list):
        raise ProblemError(422, "links must be a JSON list")
    try:
        return [WalkthroughLink.model_validate(item).model_dump() for item in parsed]
    except ValidationError as exc:
        raise ProblemError(422, "Invalid link entry", detail=str(exc))


def _share_url(w: Walkthrough) -> str | None:
    """Absolute tokened public URL, under the walkthrough's workspace; None
    unless public + minted. See `wsvc.scoped_url` for why it is not built from
    the request."""
    if w.visibility != Walkthrough.VISIBILITY_LINK or not w.share_token:
        return None
    return wsvc.scoped_url(w.workspace_id, f"/walkthrough/{w.id}?t={w.share_token}")


def _detail_payload(w: Walkthrough, *, is_owner: bool, request: HttpRequest) -> dict:
    return {
        "id": w.id,
        "title": w.title,
        "description": w.description,
        "kind": w.kind,
        "project_slug": w.project_slug,
        "visibility": w.visibility,
        "owner_email": w.owner.email,
        "size_bytes": w.size_bytes,
        "duration_sec": w.duration_sec,
        "content_type": w.content_type,
        "is_owner": is_owner,
        "links": w.links or [],
        "run_id": w.run_id,
        "narrative_slug": w.narrative_slug,
        "role": w.role,
        "cut_id": w.cut_id,
        "created_at": w.created_at,
        "updated_at": w.updated_at,
        "share_url": _share_url(w) if is_owner else None,
        "workspace": w.workspace_id,
    }


def _list_item_payload(w: Walkthrough) -> dict:
    return {
        "id": w.id,
        "title": w.title,
        "description": w.description,
        "kind": w.kind,
        "project_slug": w.project_slug,
        "visibility": w.visibility,
        "owner_email": w.owner.email,
        "size_bytes": w.size_bytes,
        "duration_sec": w.duration_sec,
        "run_id": w.run_id,
        "narrative_slug": w.narrative_slug,
        "role": w.role,
        "cut_id": w.cut_id,
        "created_at": w.created_at,
        "updated_at": w.updated_at,
    }


def _get_or_404(wid: UUID) -> Walkthrough:
    w = Walkthrough.objects.filter(pk=wid).first()
    if w is None:
        raise ProblemError(404, "Walkthrough not found", type_=TYPE_NOT_FOUND)
    return w


def _may_write(request: HttpRequest, w: Walkthrough) -> bool:
    """Editing, re-keying or deleting a walkthrough: its uploader while they are
    STILL an editor of its workspace, or an owner of that workspace.

    This was "the uploader", full stop, with no membership check — so someone
    removed from the workspace (or demoted to viewer) kept the power to flip
    their old uploads public, re-mint their links and delete them. An owner is
    added because the uploader is often an orchestrator's PAT, and somebody has
    to be able to take down a walkthrough whose uploader is gone."""
    user = request.user
    if not user.is_authenticated or w.workspace_id is None:
        return False
    if perms.can(user, w.workspace_id, perms.OWN):
        return True
    return w.owner_id == user.id and perms.can(user, w.workspace_id, perms.CONTENT_WRITE)


def _get_for_write(request: HttpRequest, wid: UUID) -> Walkthrough:
    """404 to anyone who cannot see the walkthrough as a member (existence must
    not leak — a share-token holder is a reader, not a member), 403 to a member
    who may see it but not change it."""
    w = _get_or_404(wid)
    if w.workspace_id is None or w.workspace_id not in wsvc.request_workspace_slugs(request):
        raise ProblemError(404, "Walkthrough not found", type_=TYPE_NOT_FOUND)
    if not _may_write(request, w):
        raise ProblemError(
            403,
            "Forbidden — the uploader (an editor) or a workspace owner only",
            type_=TYPE_FORBIDDEN,
        )
    return w


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response={201: WalkthroughDetailOut},
    summary="Upload a walkthrough (multipart)",
)
def upload_walkthrough(
    request: HttpRequest,
    file: UploadedFile = File(...),
    title: str = Form(""),
    kind: WalkthroughKind = Form(...),
    project_slug: str = Form(""),
    description: str = Form(""),
    visibility: WalkthroughVisibility = Form("private"),
    links: str = Form(""),
    run_id: str = Form(""),
    narrative_slug: str = Form(""),
    role: str = Form(""),
    narrative_review_id: str = Form(""),
    cut_id: str = Form(""),
    cut_scene_ids: str = Form(""),
) -> Status:
    _require_enabled()

    max_bytes = getattr(settings, "WALKTHROUGH_MAX_UPLOAD_BYTES", 75 * 1024 * 1024)
    if file.size > max_bytes:
        raise ProblemError(
            413,
            "Payload too large",
            type_=TYPE_PAYLOAD_TOO_LARGE,
            detail=f"Upload exceeds {max_bytes} bytes.",
        )

    resolved_title = (title.strip() or file.name or "untitled")[:200]
    resolved_project_slug = project_slug.strip() or None
    resolved_description = description.strip()
    resolved_links = _parse_links_field(links)
    content_type = CONTENT_TYPE_BY_KIND[kind]
    filename = FILENAME_BY_KIND[kind]
    data = file.read()

    # DDD-run grouping (optional). narrative_slug defaults to the narrative slug
    # derived from run_id when the uploader didn't send one explicitly.
    resolved_run_id = run_id.strip() or None
    resolved_narrative_slug = narrative_slug.strip() or None
    if resolved_run_id and not resolved_narrative_slug:
        resolved_narrative_slug = narrative_slug_from_run_id(resolved_run_id)
    resolved_role = role.strip() or None
    # The narrative version (ReviewRequest.id) this run rendered. Ignore a
    # malformed value rather than 500 — it's optional grouping metadata.
    resolved_review_id = None
    if narrative_review_id.strip():
        try:
            resolved_review_id = UUID(narrative_review_id.strip())
        except ValueError:
            resolved_review_id = None

    # One cut of a recorded narrative (canopy-web#1288): the recipe's cuts[].id,
    # plus the scene ids it plays (comma-separated). A cut only means something
    # on a narrative version, so it needs the version stamp — without it the
    # video could hang off nothing and would silently never show.
    resolved_cut_id = cut_id.strip()
    resolved_cut_scene_ids = [s.strip() for s in cut_scene_ids.split(",") if s.strip()]
    if resolved_cut_id:
        if not _CUT_ID_RE.fullmatch(resolved_cut_id):
            raise ProblemError(
                422, "Invalid cut id", type_=TYPE_VALIDATION,
                detail="cut_id must be 1-100 letters, digits, '.', '_' or '-' (the recipe's cuts[].id)",
            )
        if kind != "video" or resolved_review_id is None:
            raise ProblemError(
                422, "Cut needs a narrative version", type_=TYPE_VALIDATION,
                detail="a cut is a video pinned to a narrative version: send kind=video and narrative_review_id",
            )
    elif resolved_cut_scene_ids:
        raise ProblemError(
            422, "Scene ids without a cut", type_=TYPE_VALIDATION,
            detail="cut_scene_ids names the scenes of a cut; send cut_id with it",
        )

    # Backstop guard: refuse to publish a terminal DDD package artifact
    # (hero_video / docs) for a narrative that has no story-bearing version —
    # such a package renders as "no narrative". A supplied narrative_review_id is
    # proof the narrative gate ran, so it's trusted and skips the check. Mirrors
    # the plugin-side guard in scripts/ddd/upload.py so the rule holds even for
    # older plugin versions or manual uploads.
    if (
        resolved_role in _NARRATIVE_REQUIRED_ROLES
        and resolved_narrative_slug
        and resolved_review_id is None
        and not has_narrative_version(resolved_narrative_slug)
    ):
        raise ProblemError(
            409,
            "Narrative required",
            type_=TYPE_CONFLICT,
            detail=(
                f"Refusing to publish a {resolved_role!r} artifact for narrative "
                f"{resolved_narrative_slug!r}: it has no narrative version, so the run "
                f"would render as \"no narrative\". Run the ddd-narrative-review "
                f"gate for this run first, then re-upload."
            ),
        )

    # Resolve the owning workspace (tenant root) — one the uploader is ALREADY
    # in. This used to `ensure_member` the caller into the org default, making
    # an upload a way to gain EDITOR of it; see wsvc.creation_workspace.
    ws = wsvc.creation_workspace(request)
    if ws is None:
        raise ProblemError(
            422,
            "No workspace to upload into",
            type_=TYPE_VALIDATION,
            detail=wsvc.creation_refusal(request),
        )
    if not perms.can(request.user, ws, perms.CONTENT_WRITE):
        raise ProblemError(
            403,
            "Editor role required",
            type_=TYPE_FORBIDDEN,
            detail=f"uploading a walkthrough requires the editor role in {ws.slug!r}",
        )

    # Create ORM row first — if Drive fails, delete to avoid orphan row.
    w = Walkthrough.objects.create(
        title=resolved_title,
        description=resolved_description,
        kind=kind,
        project_slug=resolved_project_slug,
        owner=request.user,
        workspace=ws,
        visibility=visibility,
        links=resolved_links,
        run_id=resolved_run_id,
        narrative_slug=resolved_narrative_slug,
        role=resolved_role,
        narrative_review_id=resolved_review_id,
        cut_id=resolved_cut_id,
        cut_scene_ids=resolved_cut_scene_ids,
        drive_file_id="",
        drive_folder_id="",
        content_type=content_type,
        size_bytes=len(data),
    )

    if w.visibility == Walkthrough.VISIBILITY_LINK:
        w.ensure_share_token()

    try:
        stored = storage.store_upload(
            walkthrough_id=str(w.id),
            filename=filename,
            content_type=content_type,
            data=data,
        )
    except DriveNotConfigured as exc:
        w.delete()
        raise ProblemError(
            500,
            "Drive not configured",
            type_=TYPE_DRIVE_NOT_CONFIGURED,
            detail=str(exc),
        )
    except Exception as exc:
        w.delete()
        raise ProblemError(
            502,
            "Drive upload failed",
            type_=TYPE_DRIVE_UPLOAD_FAILED,
            detail=str(exc),
        )

    w.drive_file_id = stored.file_id
    w.drive_folder_id = stored.folder_id
    w.save(update_fields=["drive_file_id", "drive_folder_id", "updated_at"])

    # Enforce one artifact per (run_id, role): a re-upload of the same role into
    # the same run REPLACES the prior one (e.g. healing a docs page), so a run
    # can never accumulate two videos / two slideshows / two docs pages. Genuine
    # re-renders mint a fresh run_id upstream and are unaffected. Only applies
    # when both run_id and role are set — roleless / orphan uploads aren't
    # first-class run objects and are left alone. Runs only after the new row is
    # safely stored, so a failed upload never destroys the prior good artifact.
    #
    # Within THIS workspace only: run_id is a client-supplied string, so an
    # unscoped match let an upload in one tenant delete another tenant's
    # artifact (and its Drive file) by naming the same run and role.
    if resolved_run_id and resolved_role:
        superseded = Walkthrough.objects.filter(
            run_id=resolved_run_id, role=resolved_role, workspace=ws
        ).exclude(pk=w.pk)
        for old in superseded:
            # Only what you could have deleted by hand (the uploader while still
            # an editor, or a workspace owner): replacing must not be a way past
            # the rule DELETE enforces. Someone else's artifact stays; the run
            # simply holds both until its uploader or an owner removes one.
            if not _may_write(request, old):
                continue
            if old.drive_file_id:
                try:
                    storage.delete_stored(
                        file_id=old.drive_file_id, folder_id=old.drive_folder_id
                    )
                except Exception:
                    # Don't block on a Drive hiccup — an orphan Drive file is
                    # cheap to sweep later; log loudly.
                    log.exception(
                        "upload replace: drive cleanup failed (walkthrough_id=%s, "
                        "drive_file_id=%s)",
                        old.id,
                        old.drive_file_id,
                    )
            old.delete()

    return Status(
        201,
        WalkthroughDetailOut.model_validate(
            _detail_payload(w, is_owner=True, request=request)
        ),
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get(
    "/",
    response=list[WalkthroughListItemOut],
    summary="List walkthroughs",
)
def list_walkthroughs(
    request: HttpRequest,
    project: str = "",
    kind: str = "",
    mine: str = "",
) -> list[WalkthroughListItemOut]:
    _require_enabled()

    # Scope to the caller's workspace(s): the /w/{ws} prefix pins one workspace;
    # a flat call spans every workspace the caller belongs to. A row with no
    # workspace is in nobody's scope (it used to be in everybody's on the flat
    # mount — the NULL-means-allow leg).
    qs = Walkthrough.objects.select_related("owner").filter(
        workspace_id__in=wsvc.request_workspace_slugs(request)
    )
    if project:
        qs = qs.filter(project_slug=project)
    if kind in (Walkthrough.KIND_HTML, Walkthrough.KIND_VIDEO):
        qs = qs.filter(kind=kind)
    if mine == "true" and request.user.is_authenticated:
        qs = qs.filter(owner=request.user)

    return [
        WalkthroughListItemOut.model_validate(_list_item_payload(w)) for w in qs
    ]


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


@router.get(
    "/{wid}/",
    response=WalkthroughDetailOut,
    auth=None,  # Public walkthroughs load with ?t=<share_token>, no session.
    summary="Get walkthrough detail",
)
def get_walkthrough(
    request: HttpRequest, wid: UUID, t: str = "", ws: str = ""
) -> WalkthroughDetailOut:
    """`ws` is the workspace the viewer's URL names (`/w/<ws>/walkthrough/<id>`):
    a walkthrough that lives in another workspace is not at that address."""
    _require_enabled()
    w = _get_or_404(wid)
    if not w.readable_by(request):  # member of its workspace, or a matching ?t token
        raise Http404("walkthrough not found")  # don't leak private existence
    if ws and ws != w.workspace_id:
        raise Http404("walkthrough not found")
    # `is_owner` drives the edit controls and the share URL, so it answers "may
    # this caller change it", which is the write gate — not bare authorship.
    is_owner = _may_write(request, w)
    return WalkthroughDetailOut.model_validate(
        _detail_payload(w, is_owner=is_owner, request=request)
    )


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------


@router.patch(
    "/{wid}/",
    response=WalkthroughDetailOut,
    summary="Update walkthrough (uploader or workspace owner)",
)
def patch_walkthrough(
    request: HttpRequest,
    wid: UUID,
    payload: WalkthroughPatchIn,
) -> WalkthroughDetailOut:
    _require_enabled()
    w = _get_for_write(request, wid)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(w, field, value)
    w.save()

    if w.visibility == Walkthrough.VISIBILITY_LINK:
        w.ensure_share_token()  # mint on flip-to-public; keep token on flip-to-private

    w.refresh_from_db()

    return WalkthroughDetailOut.model_validate(
        _detail_payload(w, is_owner=True, request=request)
    )


# ---------------------------------------------------------------------------
# Rotate token
# ---------------------------------------------------------------------------


@router.post(
    "/{wid}/rotate-token",
    response=WalkthroughDetailOut,
    summary="Rotate the share token (uploader or workspace owner)",
)
def rotate_walkthrough_token(request: HttpRequest, wid: UUID) -> WalkthroughDetailOut:
    """Mint a fresh share token, killing every previously shared public link.

    The uploader (while an editor of the walkthrough's workspace) or a workspace
    owner. 404 to anyone who is not a member of that workspace; 403 to a member
    who may read it but not re-key it.
    """
    _require_enabled()
    w = _get_for_write(request, wid)
    w.rotate_share_token()
    return WalkthroughDetailOut.model_validate(
        _detail_payload(w, is_owner=True, request=request)
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@router.delete(
    "/{wid}/",
    response={204: None},
    summary="Delete walkthrough (uploader or workspace owner)",
)
def delete_walkthrough(request: HttpRequest, wid: UUID) -> Status:
    _require_enabled()
    w = _get_for_write(request, wid)

    if w.drive_file_id:
        try:
            storage.delete_stored(file_id=w.drive_file_id, folder_id=w.drive_folder_id)
        except DriveNotConfigured:
            # No Drive client means no orphan to clean up — still drop the
            # row so the UI matches reality.
            log.warning(
                "delete_walkthrough: drive not configured; row dropped without "
                "Drive cleanup (walkthrough_id=%s)", w.id,
            )
        except Exception:
            # Don't block the row delete on a Drive hiccup — recovering an
            # orphan Drive file is cheap; an orphan DB row blocks the user
            # from re-deleting. But log loudly so we can sweep later.
            log.exception(
                "delete_walkthrough: drive cleanup failed (walkthrough_id=%s, "
                "drive_file_id=%s)", w.id, w.drive_file_id,
            )

    w.delete()
    return Status(204, None)

