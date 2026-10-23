"""The index job: apply a pending delta version to its base's kb.sqlite, then serve it.

For each pending `KbVersion` (oldest first, one per project at a time):

1. take the base version's kb.sqlite — this instance's serving copy
   (`kb_store.local_path`), fetched once per instance as for any read; a knowledge
   base's first version has no base and starts from an empty file;
2. read the delta's records and changed parts from the content-addressed store, each
   checked against its address;
3. `canopy_kb.apply` them into a NEW file (the base is only read): the per-document
   rows replaced and the full-text index updated in place, the corpus-wide tables
   (links, entities, card search, declared tables) recomputed — the same code a full
   build runs, so the result is the full build of that content;
4. gzip and store it, then — in one transaction, and only if the project still serves
   the base — point the project at it. Anything failing before that leaves the
   published version exactly as it was, and the version `failed` with its error.

It runs where serving already runs: SQLite does the work on disk, not in memory (a
page cache of a few MB; the 1.5 GB GiveWell index applies a delta in well under the
web task's 2 GB), in a thread of its own so no request waits on it and the off-request
clock (`apps/common/ticks.py`) is never held up. The clock only KICKS it, and picks up
a version whose builder died (a deploy) after `STALE`.

When the project keeps a Drive replica (`kb_drive_replica`, off by default), the new
file is written into its Drive folder afterwards; a failure there is noted on the
version and never fails it.
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import logging
import shutil
import sqlite3
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from canopy_kb import apply, records
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import kb_drive, kb_store, kb_vectors, kb_versions
from .models import AgentProject, KbVersion

log = logging.getLogger(__name__)

#: A version `building` this long had its builder die (a deploy restarts the task).
STALE = timedelta(minutes=45)
MAX_ATTEMPTS = 3
FETCH_WORKERS = 16
TICK_S = 30

_running = threading.Lock()


def kick() -> bool:
    """Start the builder in this process unless it is already running. Never blocks."""
    if not _running.acquire(blocking=False):
        return False
    threading.Thread(target=_drain_then_release, name="kb-index", daemon=True).start()
    return True


def _drain_then_release() -> None:
    try:
        close_old_connections()
        drain()
    except Exception:  # noqa: BLE001 - a thread must not die silently
        log.exception("kb index: drain failed")
    finally:
        close_old_connections()
        _running.release()


def drain() -> list[int]:
    """Build pending versions until none is left. Returns the ids it built or failed."""
    done = []
    while True:
        version = claim()
        if version is None:
            return done
        build(version)
        done.append(version.pk)


def claim() -> KbVersion | None:
    with transaction.atomic():
        stale = timezone.now() - STALE
        for v in KbVersion.objects.select_for_update(skip_locked=True).filter(
                status=KbVersion.BUILDING, started_at__lt=stale):
            v.status = KbVersion.PENDING if v.attempts < MAX_ATTEMPTS else KbVersion.FAILED
            if v.status == KbVersion.FAILED:
                v.error = v.error or f"its builder stopped {MAX_ATTEMPTS} times"
            v.save(update_fields=["status", "error"])
        busy = KbVersion.objects.filter(status=KbVersion.BUILDING).values("project_id")
        v = (KbVersion.objects.select_for_update(skip_locked=True)
             .filter(status=KbVersion.PENDING).exclude(project_id__in=busy).order_by("id").first())
        if v is None:
            return None
        v.status, v.started_at, v.attempts, v.error = KbVersion.BUILDING, timezone.now(), v.attempts + 1, ""
        v.save(update_fields=["status", "started_at", "attempts", "error"])
        return v


def _fail(version: KbVersion, why: str) -> None:
    version.status, version.error, version.finished_at = KbVersion.FAILED, why[:4000], timezone.now()
    version.save(update_fields=["status", "error", "finished_at"])


def build(version: KbVersion) -> None:
    project = AgentProject.objects.get(pk=version.project_id)
    base = version.base
    if project.kb_version_id != (base.pk if base else None):
        _fail(version, "the project no longer serves this version's base (a newer publish or a rollback): "
                       "plan and publish again")
        return
    work = Path(tempfile.mkdtemp(prefix="kb-index-", dir=_scratch()))
    key = ""
    try:
        # A first version (no base) is built from its records alone, into an empty file.
        previous = kb_store.local_path(project) if base else None
        old = kb_store.get_blob(project, base.state_sha) if base else kb_versions.EMPTY
        new = kb_store.get_blob(project, version.state_sha)
        d = records.diff(old, new)
        ids = d["added"] + d["changed"]
        with cf.ThreadPoolExecutor(FETCH_WORKERS) as ex:
            upserts = list(ex.map(lambda i: _item(project, new["docs"][i]), ids))
        parts = {n: kb_store.get_blob(project, new["parts"][n]) for n in d["parts"]}
        aliases = parts.get(records.ALIASES)
        if aliases is None and records.ALIASES in new["parts"] and not kb_store.missing_blobs(
                project, {new["parts"][records.ALIASES]}):
            aliases = kb_store.get_blob(project, new["parts"][records.ALIASES])
        tables = records.tables(parts)
        removed_rows = _rows(previous, d["removed"]) if previous else {}
        out = work / "kb.sqlite"
        summary = apply(previous, out, upserts, d["removed"], manifest=parts.get(records.MANIFEST),
                        aliases=aliases, tables=tables)
        gz = work / "kb.sqlite.gz"
        with open(out, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        key, sha, size = kb_store.upload_file(project, gz)
        rows = {r["id"]: r for r in upserts} | removed_rows
        with transaction.atomic():
            project = AgentProject.objects.select_for_update().get(pk=project.pk)
            if project.kb_version_id != (base.pk if base else None):
                raise RuntimeError("the project moved on while this version was built")
            version.kb_key, version.kb_sha256, version.kb_bytes = key, sha, size
            version.summary = {k: v for k, v in summary.items() if k != "applied"}
            version.status, version.finished_at = KbVersion.READY, timezone.now()
            version.save(update_fields=["kb_key", "kb_sha256", "kb_bytes", "summary", "status", "finished_at"])
            kb_versions._log_ids(version, d, rows)
            kb_store.point(project, key, sha, size, version)
        kb_store.adopt(project, sha, out)  # this instance serves it without fetching it back
        key = ""
        kb_versions.prune(project)
        _replicate(project, version, gz)
        kb_vectors.sync_after_publish(project)
    except Exception as exc:  # noqa: BLE001 - recorded on the version; the published one stands
        log.exception("kb index: version %s failed", version.pk)
        if key:
            kb_store.delete_object(key)
        _fail(version, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _item(project, sha: str) -> dict:
    """A document as the index stores it: its record, with its text (a record carries
    it by reference, `canopy_kb.records`) back in place."""
    rec = kb_store.get_blob(project, sha)
    if "text_sha" not in rec:
        return rec  # published before records carried their text by reference
    try:
        text = kb_store.get_blob(project, rec["text_sha"])
    except Exception as exc:  # noqa: BLE001 - say which, not a bare KeyError
        raise RuntimeError(f"{rec.get('id')}: its text {rec['text_sha'][:12]} was not uploaded") from exc
    return records.join(rec, text)


def _rows(path: Path, ids: list[str]) -> dict[str, dict]:
    """What the base file said about the documents a version removes (for the log)."""
    if not ids:
        return {}
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        out = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in db.execute(f"SELECT id, type, title, url, date FROM items WHERE id IN ({','.join('?' * len(chunk))})",
                                chunk):
                out[r[0]] = {"type": r[1], "title": r[2], "url": r[3], "date": r[4]}
        return out
    finally:
        db.close()


def _scratch() -> str:
    root = kb_store._cache_root()
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _replicate(project, version: KbVersion, gz: Path) -> None:
    if not project.kb_drive_replica:
        return
    try:
        file_id = kb_drive.write_replica(project, gz)
        version.replica = f"drive:{file_id}"
    except Exception as exc:  # noqa: BLE001 - the replica is a convenience; the version stands
        log.warning("kb index: replica for %s failed: %s", project.pk, exc)
        version.replica = f"failed: {exc}"[:300]
    version.save(update_fields=["replica"])


def tick() -> None:
    """The off-request clock's job: start the builder if a version waits."""
    if KbVersion.objects.filter(status__in=(KbVersion.PENDING, KbVersion.BUILDING)).exists():
        kick()
