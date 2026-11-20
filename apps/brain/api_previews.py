"""`/api/brain/previews/`: a person's preview snapshots (`snapshots.py`), the `preview:<id>` source every brain screen takes
(`view.py`).

PERSON-ONLY, refused as the preview refuses (`api_preview.the_person`: an agent's login, a turn's or caller token, a
runner, an OAuth client, a site's token, an MCP call; none is an MCP tool and no act-as token reaches them). A snapshot is
its owner's: anyone else's is not found. Making one writes that row and nothing else."""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc

from . import snapshots, view
from .api_preview import MAX_WINDOW, the_person
from .models import BrainPreviewSnapshot
from .schemas import PreviewOutputsIn, PreviewSnapshotIn, PreviewSnapshotOut

router = Router(auth=session_auth, tags=["brain"])


def source_of(request: HttpRequest, text: str) -> view.Source:
    """`view.resolve`, its refusals as problems: a malformed source is a 422, a preview not yours a 404."""
    try:
        return view.resolve(request, text)
    except view.Invalid as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    except view.NotFound as e:
        raise ProblemError(404, "No such preview", type_=TYPE_NOT_FOUND) from e


def _out(s: BrainPreviewSnapshot) -> dict:
    return {"id": s.pk, "source": f"preview:{s.pk}", "window": {"since": s.since, "until": s.until}, "view": s.view,
            "created_at": s.created_at, "expires_at": s.expires_at, "sessions": len(s.session_ids or []),
            "outputs": len(s.outputs or []), "truncated": s.truncated}


def _mine(request: HttpRequest, preview_id: int) -> tuple:
    user = the_person(request)
    snap = snapshots.live_for(user, preview_id)
    if snap is None:
        raise ProblemError(404, "No such preview", type_=TYPE_NOT_FOUND)
    return user, snap


@router.post("/brain/previews/", response={200: PreviewSnapshotOut, **refusals(403, 422)},
             summary="Run a preview: what learning would do over your sessions in a window, kept for a week")
@gate("self", kind="action")  # your own dry run; writes the snapshot row and nothing else
def create_brain_preview(request: HttpRequest, payload: PreviewSnapshotIn) -> dict:
    """Computes, over the sessions you read created or active in the window (the last day by default, a week at most),
    who is in each, whether each class could read it now and if everyone agreed, and the project links canopy would
    make, exactly as the live code would; and keeps it, so every brain screen can show it with `source=preview:<id>`.
    Nothing of the brain itself is written."""
    user = the_person(request)
    until = payload.until or timezone.now()
    since = payload.since or until - dt.timedelta(days=1)
    if since >= until or until - since > MAX_WINDOW:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail="since must be before until, and the window a week at most")
    snap = snapshots.create(user, wsvc.request_member_slugs(request), since=since, until=until, view=payload.view)
    return _out(snap)


@router.get("/brain/previews/", response={200: list[PreviewSnapshotOut], **refusals(403)},
            summary="Your previews, newest first")
@gate("self")
def list_brain_previews(request: HttpRequest) -> list[dict]:
    user = the_person(request)
    rows = BrainPreviewSnapshot.objects.filter(owner=user, expires_at__gt=timezone.now()).order_by("-created_at")
    return [_out(s) for s in rows.defer("payload")[:50]]


@router.delete("/brain/previews/{preview_id}/", response={204: None, **refusals(403, 404)},
               summary="Delete one of your previews")
@gate("self", kind="action")
def delete_brain_preview(request: HttpRequest, preview_id: int):
    _user, snap = _mine(request, preview_id)
    snap.delete()
    return Status(204, None)


@router.put("/brain/previews/{preview_id}/outputs/", response={200: PreviewSnapshotOut, **refusals(403, 404, 422)},
            summary="Publish recommendations to one of your previews")
@gate("self", kind="content")  # recommendations made on your machine, shown only to you, in the preview
def put_brain_preview_outputs(request: HttpRequest, preview_id: int, payload: PreviewOutputsIn) -> dict:
    """Replaces the preview's recommendations: would-be `system` outputs from `/canopy:brain-preview --publish <id>`,
    shown where outputs are shown when the preview is the source. Each must cite sessions of the preview that you still
    read; words are scrubbed of secrets, object keys too. Only you see them."""
    user, snap = _mine(request, preview_id)
    readable = view.visible_sessions(user, snap)
    try:
        snapshots.put_outputs(snap, [o.model_dump(mode="json") for o in payload.outputs], readable=readable)
    except snapshots.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _out(snap)
