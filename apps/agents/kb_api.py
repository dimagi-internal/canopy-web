"""A workspace project's knowledge base: its version log, the steward's curation
store, its sources and — for a Drive source — the owner's review of what may be read.

    GET  …/kb/versions                  the log (each version a whole file the steward
                                        built, and the curation state it was built from)
    GET  …/kb/file                      a download of the file it serves now
    POST …/kb/versions/{n}/rollback     serve version n's file (and state) again
    POST …/kb/results                   presigned PUTs for step results the store lacks
    POST …/kb/objects/have|links        which results, records, originals are stored; downloads
    PUT  …/kb/settings                  the Drive replica option (off by default)
    GET/POST …/kb/sources               where its documents come from
    POST …/kb/sources/{id}/inventory    a Drive source's metadata listing (the steward)
    GET  …/kb/sources/{id}/files        its files (`?readable=true`: what the steward may read)
    POST …/kb/sources/{id}/files/{f}/hold   a spreadsheet's headers look like personal data
    GET  …/kb/review                    the owner's review page
    POST …/kb/review/approvals          the owner switches files and folders on and off
    POST …/kb/readers                   the owner names who may query a Drive KB

The whole-file publish (`…/kb/upload`, `…/kb/publish`) stays in `fleet_api.py`.
Services: `kb_versions`, `kb_drive`. canopy-web never builds an index (#1971).
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate

from . import kb_drive, kb_store, kb_versions
from .fleet_api import _kb_call, _workspace_project_or_404
from .gates import agent_for_work
from .models import KbDriveFile, KbSource, KbVersion
from .schemas import (
    AgentProjectOut,
    KbApprovalsIn,
    KbApprovalsOut,
    KbBlobsIn,
    KbDriveFileOut,
    KbFileOut,
    KbHoldIn,
    KbInventoryIn,
    KbObjectLinksOut,
    KbObjectsHaveOut,
    KbObjectsIn,
    KbOriginalsOut,
    KbReadersIn,
    KbReviewOut,
    KbReviewSourceOut,
    KbSettingsIn,
    KbSourceIn,
    KbSourceOut,
    KbVersionOut,
)

router = Router(auth=session_auth, tags=["knowledge bases"])

_BASE = "/workspace-projects/{workspace}/{handle}/kb"


def _steward(request: HttpRequest, workspace: str, handle: str):
    project = _workspace_project_or_404(request, workspace, handle)
    agent_for_work(request, project.agent.slug)  # the steward's write gate: its curator publishes
    return project


def _reader(request: HttpRequest, workspace: str, handle: str):
    """A member who may see this KB's sources and files: a Drive KB's are its owner's,
    its steward's and its named readers' only (same 404 as no project)."""
    project = _workspace_project_or_404(request, workspace, handle)
    if not kb_drive.may_read(request.user, project):
        raise HttpError(404, f"workspace project {workspace}/{handle} not found")
    return project


def _owner(request: HttpRequest, workspace: str, handle: str):
    project = _reader(request, workspace, handle)
    if not kb_drive.may_decide(request.user, project):
        raise HttpError(403, "only the project's owner decides which of its files are read")
    return project


def _conflict(fn):
    try:
        return _kb_call(fn)
    except kb_versions.KbConflict as exc:
        raise HttpError(409, str(exc)) from exc
    except kb_drive.DriveSourceError as exc:
        raise HttpError(422, str(exc)) from exc


def _version_out(project, v: KbVersion) -> KbVersionOut:
    return KbVersionOut(
        number=v.number, kind=v.kind, status=v.status, base=v.base.number if v.base_id else None,
        rollback_of=v.rollback_of.number if v.rollback_of_id else None, added=v.added, changed=v.changed,
        removed=v.removed, uploaded_bytes=v.uploaded_bytes, kb_bytes=v.kb_bytes, summary=v.summary or {},
        error=v.error, published_by=v.published_by, created_at=v.created_at, finished_at=v.finished_at,
        replica=v.replica, curation_sha=v.curation_sha, current=project.kb_version_id == v.pk,
        can_roll_back=v.status == KbVersion.READY and bool(v.kb_key) and project.kb_key != v.kb_key)


def _source(project, source_id: int) -> KbSource:
    source = KbSource.objects.filter(project=project, pk=source_id).first()
    if source is None:
        raise HttpError(404, f"no source {source_id} on this knowledge base")
    return source


def _file_out(row: KbDriveFile, queued: set[str]) -> KbDriveFileOut:
    return KbDriveFileOut(
        file_id=row.file_id, parent_id=row.parent_id, name=row.name, path=row.path, mime_type=row.mime_type,
        is_folder=row.is_folder, size=row.size, owner=row.owner, modified_time=row.modified_time, md5=row.md5,
        drive_version=row.drive_version, web_url=row.web_url, readers=row.readers or [],
        skip_reason=row.skip_reason, flags=row.flags or [], decision=row.decision,
        decided_by=row.decided_by.email if row.decided_by_id else "", decided_at=row.decided_at,
        auto_approve=row.auto_approve, held_headers=row.held_headers or [], hold_released=row.hold_released,
        present=row.present, first_seen_at=row.first_seen_at, queued=row.file_id in queued,
        indexable=kb_drive.is_indexable(row))


# ---- curation: what a refresh needs from the last one, on any machine ----
# Steward only, reads included: for a Drive source these are its owner's text.

@router.post(f"{_BASE}/results", response=KbOriginalsOut,
             summary="Presigned uploads for the step results the store lacks")
@gate("agent.work", kind="content")
def kb_upload_results(request: HttpRequest, workspace: str, handle: str, payload: KbBlobsIn) -> KbOriginalsOut:
    """Each result gzipped, under its step key (`canopy_kb.curate`); the PUT carries the
    gzip's sha256, which storage checks."""
    project = _steward(request, workspace, handle)
    blobs = [b.model_dump() for b in payload.blobs]
    return KbOriginalsOut(**_conflict(lambda: kb_store.results_upload(project, blobs)))


@router.post(f"{_BASE}/objects/have", response=KbObjectsHaveOut,
             summary="Which step results or originals are stored")
@gate("agent.work", kind="action")  # changes nothing
def kb_objects_have(request: HttpRequest, workspace: str, handle: str, payload: KbObjectsIn) -> KbObjectsHaveOut:
    project = _steward(request, workspace, handle)
    return KbObjectsHaveOut(have=_conflict(lambda: kb_store.stored(project, payload.store, payload.keys)))


@router.post(f"{_BASE}/objects/links", response=KbObjectLinksOut,
             summary="Presigned downloads of stored step results or originals")
@gate("agent.work", kind="action")  # changes nothing
def kb_objects_links(request: HttpRequest, workspace: str, handle: str, payload: KbObjectsIn) -> KbObjectLinksOut:
    project = _steward(request, workspace, handle)
    return KbObjectLinksOut(**_conflict(lambda: kb_store.links(project, payload.store, payload.keys)))


@router.get(f"{_BASE}/versions", response=list[KbVersionOut],
            summary="A knowledge base's version log, newest first")
@gate("member")
def kb_list_versions(request: HttpRequest, workspace: str, handle: str, limit: int = 50) -> list[KbVersionOut]:
    project = _reader(request, workspace, handle)
    qs = (KbVersion.objects.filter(project=project).select_related("base", "rollback_of")
          .order_by("-number")[:max(1, min(int(limit), 500))])
    return [_version_out(project, v) for v in qs]


@router.get(f"{_BASE}/file", response=KbFileOut,
            summary="A short-lived download of the knowledge base file a project serves now")
@gate("member")
def kb_file(request: HttpRequest, workspace: str, handle: str) -> KbFileOut:
    """For a curator's local copy or an offline reader (`canopy kb pull`); readers
    normally call the project's MCP tools and download nothing."""
    project = _reader(request, workspace, handle)
    return KbFileOut(**_conflict(lambda: kb_store.file_link(project)))


@router.post(f"{_BASE}/versions/{{number}}/rollback", response=KbVersionOut,
             summary="Serve an earlier version of a knowledge base again")
@gate("agent.work", kind="content")
def kb_rollback(request: HttpRequest, workspace: str, handle: str, number: int) -> KbVersionOut:
    """A new version of the log pointing at version `number`'s file (kept for the newest
    versions only: `can_roll_back` says which), with the curation state it was built
    from: the next refresh starts from that state."""
    project = _steward(request, workspace, handle)
    version = _conflict(lambda: kb_versions.rollback(project, number, published_by=kb_versions.publisher(request.user)))
    project.refresh_from_db()
    return _version_out(project, version)


@router.put(f"{_BASE}/settings", response=AgentProjectOut,
            summary="Set a knowledge base's options (the Drive replica)")
@gate("agent.work", setting=True)
def kb_settings(request: HttpRequest, workspace: str, handle: str, payload: KbSettingsIn) -> AgentProjectOut:
    """`drive_replica`: after each version, canopy-web writes its kb.sqlite.gz into the
    project's Drive folder (shared with canopy-web's service account). Off by default:
    readers query the MCP and need no copy."""
    project = _steward(request, workspace, handle)
    project.kb_drive_replica = payload.drive_replica
    project.save(update_fields=["kb_drive_replica", "updated_at"])
    return AgentProjectOut.model_validate(project)


# ---- sources ----

@router.get(f"{_BASE}/sources", response=list[KbSourceOut], summary="Where a knowledge base's documents come from")
@gate("member")
def kb_list_sources(request: HttpRequest, workspace: str, handle: str) -> list[KbSourceOut]:
    project = _reader(request, workspace, handle)
    return [KbSourceOut.model_validate(s) for s in project.kb_sources.all()]


@router.post(f"{_BASE}/sources", response=KbSourceOut, summary="Add or update a knowledge-base source, by name")
@gate("agent.work", setting=True)
def kb_put_source(request: HttpRequest, workspace: str, handle: str, payload: KbSourceIn) -> KbSourceOut:
    """`web` (a curator's crawler) or `drive_folders` (`config.folders`: Drive folder ids).
    Nothing in a Drive source is read until its owner approves it on the review page."""
    project = _steward(request, workspace, handle)
    config = dict(payload.config)
    if payload.kind == KbSource.DRIVE:
        folders = [str(f).strip() for f in config.get("folders") or [] if str(f).strip()]
        if not folders:
            raise HttpError(422, "a drive_folders source names at least one folder id in config.folders")
        config["folders"] = folders
    source, _ = KbSource.objects.update_or_create(project=project, name=payload.name,
                                                  defaults={"kind": payload.kind, "config": config})
    return KbSourceOut.model_validate(source)


@router.post(f"{_BASE}/sources/{{source_id}}/inventory", response=dict,
             summary="Record a Drive source's metadata listing (names and types only, never contents)")
@gate("agent.work", kind="content")
def kb_inventory(request: HttpRequest, workspace: str, handle: str, source_id: int, payload: KbInventoryIn) -> dict:
    """Every file and folder under the source's folders, including those folders, as
    Drive's metadata shows them. New files get the owner's decision rules; ones gone
    from Drive are marked so and leave the index at the next version."""
    project = _steward(request, workspace, handle)
    source = _source(project, source_id)
    files = [f.model_dump() for f in payload.files]
    return _conflict(lambda: kb_drive.ingest(source, files, payload.page_token))


@router.get(f"{_BASE}/sources/{{source_id}}/files", response=list[KbDriveFileOut],
            summary="A Drive source's files (readable=true: the approved ones the steward may read)")
@gate("member")
def kb_source_files(request: HttpRequest, workspace: str, handle: str, source_id: int,
                    readable: bool = False) -> list[KbDriveFileOut]:
    project = _reader(request, workspace, handle)
    source = _source(project, source_id)
    rows = kb_drive.readable(source) if readable else source.drive_files.all()
    queued = {r.file_id for r in kb_drive.queue(source)}
    return [_file_out(r, queued) for r in rows.select_related("decided_by")]


@router.post(f"{_BASE}/sources/{{source_id}}/files/{{file_id}}/hold", response=KbDriveFileOut,
             summary="Hold an approved spreadsheet whose column headers look like personal data")
@gate("agent.work", kind="content")
def kb_hold(request: HttpRequest, workspace: str, handle: str, source_id: int, file_id: str,
            payload: KbHoldIn) -> KbDriveFileOut:
    """It is not indexed until the owner releases it on the review page. An empty list
    clears a hold the steward set by mistake (never one the owner released)."""
    project = _steward(request, workspace, handle)
    row = _conflict(lambda: kb_drive.hold(_source(project, source_id), file_id, payload.headers))
    return _file_out(row, set())


# ---- the owner's review ----

@router.get(f"{_BASE}/review", response=KbReviewOut,
            summary="A knowledge base's sources and its owner's review of each Drive file")
@gate("member")
def kb_review(request: HttpRequest, workspace: str, handle: str) -> KbReviewOut:
    project = _reader(request, workspace, handle)
    sources = []
    for s in project.kb_sources.all():
        queued = {r.file_id for r in kb_drive.queue(s)} if s.kind == KbSource.DRIVE else set()
        files = [_file_out(r, queued) for r in s.drive_files.select_related("decided_by")]
        sources.append(KbReviewSourceOut(**KbSourceOut.model_validate(s).model_dump(), files=files))
    return KbReviewOut(
        address=project.address, name=project.name,
        owner_email=project.owner_user.email if project.owner_user_id else None,
        steward_slug=project.agent.slug, may_decide=kb_drive.may_decide(request.user, project), sources=sources,
        readers=sorted(project.kb_readers.values_list("email", flat=True)),
        drive_replica=project.kb_drive_replica, version=project.kb_version_number)


@router.post(f"{_BASE}/review/approvals", response=KbApprovalsOut,
             summary="Switch a Drive source's files and folders on or off (the project's owner)")
@gate("member", setting=True,
      viewer_write="the project's OWNER decides which of their own files an agent may read, whatever their workspace "
                   "role; nobody else may, the steward agent included")
def kb_approve(request: HttpRequest, workspace: str, handle: str, payload: KbApprovalsIn) -> KbApprovalsOut:
    """A folder's decision reaches everything under it (an excluded file stays excluded
    when its folder is approved). `auto_approve` on a folder admits files that appear in
    it later; without it they wait in the "new since approval" queue. `release_hold`
    lets a spreadsheet the header check held be indexed."""
    project = _owner(request, workspace, handle)
    source = _source(project, payload.source)
    return KbApprovalsOut(**_conflict(lambda: kb_drive.decide(
        source, request.user, [d.model_dump(exclude_unset=True) for d in payload.files])))


@router.post(f"{_BASE}/readers", response=KbReviewOut,
             summary="Name who may query a knowledge base with a Drive source (the project's owner)")
@gate("member", setting=True,
      viewer_write="the project's OWNER decides who may query a knowledge base made from their files, whatever "
                   "their workspace role")
def kb_readers(request: HttpRequest, workspace: str, handle: str, payload: KbReadersIn) -> KbReviewOut:
    project = _owner(request, workspace, handle)
    for email in payload.add:
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            raise HttpError(422, f"no canopy account for {email}")
        project.kb_readers.add(user)
    for email in payload.remove:
        project.kb_readers.remove(*User.objects.filter(email__iexact=email.strip()))
    return kb_review(request, workspace, handle)
