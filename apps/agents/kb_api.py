"""A workspace project's knowledge base: its version log, the steward's curation
store and its sources.

    GET  …/kb/versions                  the log (each version a whole file the steward
                                        built, and the curation state it was built from)
    GET  …/kb/file                      a download of the file it serves now
    POST …/kb/versions/{n}/rollback     serve version n's file (and state) again
    POST …/kb/results                   presigned PUTs for step results the store lacks
    POST …/kb/objects/have|links        which results, records, originals are stored; downloads
    PUT  …/kb/settings                  the Drive replica option (off by default)
    GET/POST …/kb/sources               where its documents come from (a curator's crawler)

The whole-file publish (`…/kb/upload`, `…/kb/publish`) stays in `fleet_api.py`.
Services: `kb_versions`, `kb_store`. canopy-web never builds an index (#1971).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate

from . import kb_store, kb_versions
from .fleet_api import _kb_call, _workspace_project_or_404
from .gates import agent_for_work
from .models import KbSource, KbVersion
from .schemas import (
    AgentProjectOut,
    KbBlobsIn,
    KbFileOut,
    KbObjectLinksOut,
    KbObjectsHaveOut,
    KbObjectsIn,
    KbOriginalsOut,
    KbSettingsIn,
    KbSourceIn,
    KbSourceOut,
    KbVersionOut,
)

router = Router(auth=session_auth, tags=["knowledge bases"])

_BASE = "/workspace-projects/{workspace}/{handle}/kb"


def _steward(request: HttpRequest, workspace: str, handle: str):
    project = _workspace_project_or_404(request, workspace, handle)
    if project.agent_id is None:
        raise HttpError(409, f"{project.address} has no lead agent to keep its knowledge base")
    agent_for_work(request, project.agent.slug)  # the steward's write gate: its curator publishes
    return project


def _conflict(fn):
    try:
        return _kb_call(fn)
    except kb_versions.KbConflict as exc:
        raise HttpError(409, str(exc)) from exc


def _version_out(project, v: KbVersion) -> KbVersionOut:
    kb = project.knowledge_base
    head_id, head_key = (kb.kb_version_id, kb.kb_key) if kb else (None, "")
    return KbVersionOut(
        number=v.number, kind=v.kind, status=v.status, base=v.base.number if v.base_id else None,
        rollback_of=v.rollback_of.number if v.rollback_of_id else None, added=v.added, changed=v.changed,
        removed=v.removed, uploaded_bytes=v.uploaded_bytes, kb_bytes=v.kb_bytes, summary=v.summary or {},
        error=v.error, published_by=v.published_by, created_at=v.created_at, finished_at=v.finished_at,
        replica=v.replica, curation_sha=v.curation_sha, current=head_id == v.pk,
        can_roll_back=v.status == KbVersion.READY and bool(v.kb_key) and head_key != v.kb_key)


# ---- curation: what a refresh needs from the last one, on any machine ----
# Steward only, reads included.

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
    project = _workspace_project_or_404(request, workspace, handle)
    qs = (KbVersion.objects.filter(project=project).select_related("base", "rollback_of")
          .order_by("-number")[:max(1, min(int(limit), 500))])
    return [_version_out(project, v) for v in qs]


@router.get(f"{_BASE}/file", response=KbFileOut,
            summary="A short-lived download of the knowledge base file a project serves now")
@gate("member")
def kb_file(request: HttpRequest, workspace: str, handle: str) -> KbFileOut:
    """For a curator's local copy or an offline reader (`canopy kb pull`); readers
    normally call the project's MCP tools and download nothing."""
    project = _workspace_project_or_404(request, workspace, handle)
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
    kb = kb_store.knowledge_base(project, create=True)
    kb.kb_drive_replica = payload.drive_replica
    kb.save(update_fields=["kb_drive_replica", "updated_at"])
    return AgentProjectOut.model_validate(project)


# ---- sources ----

@router.get(f"{_BASE}/sources", response=list[KbSourceOut], summary="Where a knowledge base's documents come from")
@gate("member")
def kb_list_sources(request: HttpRequest, workspace: str, handle: str) -> list[KbSourceOut]:
    project = _workspace_project_or_404(request, workspace, handle)
    return [KbSourceOut.model_validate(s) for s in project.kb_sources.all()]


@router.post(f"{_BASE}/sources", response=KbSourceOut, summary="Add or update a knowledge-base source, by name")
@gate("agent.work", setting=True)
def kb_put_source(request: HttpRequest, workspace: str, handle: str, payload: KbSourceIn) -> KbSourceOut:
    """`web`: a curator's crawler; `config` notes what it reads. The only kind."""
    project = _steward(request, workspace, handle)
    source, _ = KbSource.objects.update_or_create(project=project, name=payload.name,
                                                  defaults={"kind": payload.kind, "config": dict(payload.config)})
    return KbSourceOut.model_validate(source)
