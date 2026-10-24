"""A workspace project's knowledge base as a version log of whole files.

canopy-web never builds an index (#1971: building in its web process ran it out of
memory). The machine that refreshes builds kb.sqlite (`canopy_kb.curate`, or `canopy kb
build`) and publishes it whole (`publish_full`): canopy-web checks it, stores it, serves
it, and records it as a version.

A version carries the curation state it was built from (`curation_sha`, a root in the
steward's results store; `canopy_kb.curate.State`): a refresh starts from the head
version's, so the state and the file readers see can never disagree, and a `rollback`
rolls the state back with the file. A publish names the version it was made from
(`base`) and is refused when another landed since (`KbConflict`): one refresh wins, and
the loser keeps every result it paid for.

What changed is the file's own history (its `changes` table, which `whats_new` reads):
the builder records only documents whose content changed.

Only the newest `KEEP_FILES` versions keep their kb.sqlite.gz (`prune`); older ones
stay in the log and can no longer be rolled back to. A project with a Drive replica
(off by default) has each new file copied into its Drive folder afterwards.
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import kb_store
from .models import Agent, AgentProject, KbVersion

log = logging.getLogger(__name__)

KEEP_FILES = 5
#: A version still on its way (a delta canopy-web was to build, before #1971).
_LIVE = (KbVersion.PENDING, KbVersion.BUILDING)


class KbConflict(RuntimeError):
    """The log moved since the steward started (another publish, a rollback)."""


def publisher(user) -> str:
    """Who published: the agent whose own login sent it, else the person's address."""
    if not getattr(user, "is_authenticated", False):
        return ""
    agent = Agent.objects.filter(user=user).only("slug").first()
    return agent.slug if agent else (user.email or "")


def _next_number(project) -> int:
    last = KbVersion.objects.filter(project=project).order_by("-number").values_list("number", flat=True).first()
    return (last or 0) + 1


def _counts(changes: dict | None) -> dict:
    out = {}
    for k in ("added", "changed", "removed"):
        v = (changes or {}).get(k)
        out[k] = max(0, int(v)) if isinstance(v, int) and not isinstance(v, bool) else 0
    return out


def publish_full(project, key: str, sha256: str, *, base: int | None, curation: str = "", published_by: str = "",
                 changes: dict | None = None):
    """The whole-file publish (`kb_store.publish`), recorded as a FULL version made from
    version `base` (None: the first). `curation` is the state it was built from, already
    in the results store. `changes` counts the documents whose content changed."""
    if curation and not kb_store.stored(project, "results", [curation]):
        raise kb_store.KbPublishError(f"curation state {curation[:12]} is not stored: upload it first")
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        kb = kb_store.knowledge_base(project, lock=True)
        head = kb.kb_version if kb else None
        if (head.number if head else None) != base:
            raise KbConflict(f"the knowledge base is at version {head.number if head else 'none'}, not {base}: "
                             "another publish or a rollback landed since this refresh started; refresh again")
        # A delta canopy-web was still to build (before #1971) never will be.
        KbVersion.objects.filter(project=project, status__in=_LIVE).update(
            status=KbVersion.FAILED, error="superseded by a whole-file publish", finished_at=timezone.now())
        project = kb_store.publish(project, key, sha256)
        kb = project.knowledge_base
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.FULL, status=KbVersion.READY,
            base=head, curation_sha=curation, kb_key=kb.kb_key, kb_sha256=kb.kb_sha256,
            kb_bytes=kb.kb_bytes, uploaded_bytes=kb.kb_bytes or 0, published_by=published_by,
            started_at=timezone.now(), finished_at=timezone.now(), **_counts(changes))
        kb.kb_version = version
        kb.save(update_fields=["kb_version"])
        if kb.kb_drive_replica:
            transaction.on_commit(lambda: _replicate_later(project.pk, version.pk))
    prune(project)
    return project, version


def rollback(project, number: int, *, published_by: str = "") -> KbVersion:
    """Serve version `number`'s file again, with the state it was built from, as a new
    version of the log."""
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        kb = kb_store.knowledge_base(project, lock=True)
        target = KbVersion.objects.filter(project=project, number=number).first()
        if target is None:
            raise kb_store.KbPublishError(f"no version {number}")
        if target.status != KbVersion.READY or not target.kb_key:
            raise kb_store.KbPublishError(f"version {number} cannot be served again "
                                          f"({'its file was pruned' if target.status == KbVersion.READY else target.status})")
        head = kb.kb_version if kb else None
        if head is not None and head.kb_key == target.kb_key:
            raise kb_store.KbPublishError(f"version {number} is what the project serves already")
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.ROLLBACK, status=KbVersion.READY,
            base=head, rollback_of=target, curation_sha=target.curation_sha, kb_key=target.kb_key,
            kb_sha256=target.kb_sha256, kb_bytes=target.kb_bytes, summary=target.summary,
            published_by=published_by, started_at=timezone.now(), finished_at=timezone.now())
        kb_store.point(project, target.kb_key, target.kb_sha256, target.kb_bytes or 0, version)
    return version


def prune(project) -> list[str]:
    """Delete the files of versions beyond the newest KEEP_FILES ready ones (and never
    the head's). Their log entries stay."""
    ready = list(KbVersion.objects.filter(project=project, status=KbVersion.READY).exclude(kb_key="")
                 .order_by("-number"))
    keep = {v.kb_key for v in ready[:KEEP_FILES]}
    kb = kb_store.knowledge_base(project)
    if kb is not None and kb.kb_key:
        keep.add(kb.kb_key)
    gone = sorted({v.kb_key for v in ready[KEEP_FILES:]} - keep)
    for key in gone:
        kb_store.delete_object(key)
    if gone:
        KbVersion.objects.filter(project=project, kb_key__in=gone).update(kb_key="")
    return gone


class ReplicaError(RuntimeError):
    """The Drive replica cannot be written (no folder, no service-account key)."""


def write_replica(project, gz: Path) -> str:
    """Write a version's kb.sqlite.gz into the project's Drive folder with canopy-web's
    service account (the folder must be shared with it), replacing the last one."""
    from google.oauth2 import service_account  # noqa: PLC0415 — the Google client loads only for a replica
    from googleapiclient.discovery import build  # noqa: PLC0415 — the Google client loads only for a replica
    from googleapiclient.http import MediaFileUpload  # noqa: PLC0415 — the Google client loads only for a replica

    if not project.drive_folder_id:
        raise ReplicaError("the project has no Drive folder")
    raw = getattr(settings, "CANOPY_DRIVE_SA_KEY_JSON", "") or ""
    if not raw:
        raise ReplicaError("no CANOPY_DRIVE_SA_KEY_JSON on this deployment")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(gz), mimetype="application/gzip", resumable=True)
    made = drive.files().create(body={"name": "kb.sqlite.gz", "parents": [project.drive_folder_id]},
                                media_body=media, fields="id", supportsAllDrives=True).execute()
    kb = project.knowledge_base
    previous = kb.kb_drive_replica_file_id
    kb.kb_drive_replica_file_id = made["id"]
    kb.save(update_fields=["kb_drive_replica_file_id"])
    if previous:
        try:
            drive.files().delete(fileId=previous, supportsAllDrives=True).execute()
        except Exception:  # noqa: BLE001 - a stale replica costs Drive space, not correctness
            pass
    return made["id"]


def _replicate_later(project_pk: int, version_pk: int) -> None:
    """Copy a version's file into the project's Drive folder (`kb_drive_replica`, off by
    default), off the request: a stream from storage to Drive, never a build."""
    threading.Thread(target=_replicate, args=(project_pk, version_pk), name="kb-replica", daemon=True).start()


def _replicate(project_pk: int, version_pk: int) -> None:
    try:
        project, version = AgentProject.objects.get(pk=project_pk), KbVersion.objects.get(pk=version_pk)
        with tempfile.TemporaryDirectory(prefix="kb-replica-") as td:
            gz = Path(td) / "kb.sqlite.gz"
            body = kb_store._client().get_object(Bucket=kb_store._bucket(), Key=version.kb_key)["Body"]
            with open(gz, "wb") as fh:
                for chunk in iter(lambda: body.read(1 << 20), b""):
                    fh.write(chunk)
            version.replica = f"drive:{write_replica(project, gz)}"
    except Exception as exc:  # noqa: BLE001 - the replica is a convenience; the version stands
        log.warning("kb replica for project %s failed: %s", project_pk, exc)
        version = KbVersion.objects.filter(pk=version_pk).first()
        if version is None:
            return
        version.replica = f"failed: {exc}"[:300]
    finally:
        close_old_connections()
    version.save(update_fields=["replica"])
