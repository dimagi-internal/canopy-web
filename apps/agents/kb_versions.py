"""A workspace project's knowledge base as a version log of whole files.

canopy-web never builds an index (#1971: building in its web process ran it out of
memory). The machine that refreshes builds kb.sqlite (`canopy_kb.curate`, or `canopy kb
build`) and publishes it whole (`publish_full`): canopy-web stores it, serves it, and
records it as a version.

A publish names its STATE — every document record's and corpus part's sha256
(`canopy_kb.records.state`) — so `plan(state)` can say a refresh changed nothing (no
upload, no version) and a version can list what it added, changed and removed
(`KbChange`, which `whats_new` reads; the builder reports content changes, else the
state diff). A Drive-sourced knowledge base's state is vetted: a document its owner
has not approved is refused (`kb_drive.check_ids`). `rollback` points the project at an
earlier version's file.

Only the newest `KEEP_FILES` versions keep their kb.sqlite.gz (`prune`); older ones
stay in the log and can no longer be rolled back to. A project with a Drive replica
(off by default) has each new file copied into its Drive folder afterwards.
"""
from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from canopy_kb import records
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import kb_drive, kb_store
from .models import Agent, AgentProject, KbChange, KbVersion

log = logging.getLogger(__name__)

KEEP_FILES = 5
#: A version still on its way: one at a time per project.
_LIVE =(KbVersion.PENDING, KbVersion.BUILDING)
#: What a knowledge base with no version yet holds.
EMPTY = {"docs": {}, "parts": {}}


class KbConflict(RuntimeError):
    """The log moved since the steward planned (another publish, a rollback)."""


def _state_ok(state) -> dict:
    if not isinstance(state, dict) or not isinstance(state.get("docs"), dict) or not isinstance(state.get("parts"), dict):
        raise kb_store.KbPublishError("state must be {docs: {item_id: sha256}, parts: {name: sha256}}")
    for k, v in list(state["docs"].items()) + list(state["parts"].items()):
        if not isinstance(k, str) or not k or not kb_store._is_sha256(str(v)):
            raise kb_store.KbPublishError(f"state: {str(k)[:80]!r} is not addressed by a sha256")
    return state


def publisher(user) -> str:
    """Who published: the agent whose own login sent it, else the person's address."""
    if not getattr(user, "is_authenticated", False):
        return ""
    agent = Agent.objects.filter(user=user).only("slug").first()
    return agent.slug if agent else (user.email or "")


def head_state(project) -> dict | None:
    """The state the project serves: EMPTY before its first version, None when its head
    is a whole-file publish that carried no state (publish the whole file once more)."""
    head = project.kb_version
    if head is None:
        return EMPTY
    if not head.state_sha:
        return None
    return kb_store.get_blob(project, head.state_sha)


def _next_number(project) -> int:
    last = KbVersion.objects.filter(project=project).order_by("-number").values_list("number", flat=True).first()
    return (last or 0) + 1


def plan(project, state: dict) -> dict:
    """What publishing `state` would change, and what to upload for it."""
    _state_ok(state)
    head = project.kb_version
    base = head.number if head else None
    current = head_state(project)
    if current is None:
        return {"base": base, "full_required": True, "unchanged": False, "added": 0, "changed": 0, "removed": 0,
                "upload_docs": [], "upload_parts": [],
                "reason": "the published knowledge base carries no state yet: publish the whole file once"}
    d = records.diff(current, state)
    unchanged = not (d["added"] or d["changed"] or d["removed"] or d["parts"])
    return {"base": base, "full_required": False, "unchanged": unchanged, "added": len(d["added"]),
            "changed": len(d["changed"]), "removed": len(d["removed"]),
            "upload_docs": d["added"] + d["changed"], "upload_parts": d["parts"], "reason": ""}


def publish_full(project, key: str, sha256: str, state: dict | None = None, *, published_by: str = "",
                 changes: dict | None = None, check_ids=None):
    """The whole-file publish (`kb_store.publish`), recorded as a FULL version."""
    if state is not None:
        _state_ok(state)
    if check_ids is not None:  # a Drive-sourced KB: every document it names must be approved
        if state is None:
            raise kb_store.KbPublishError("a knowledge base with a Drive source publishes with its state, "
                                          "so its documents can be checked against the owner's approvals")
        check_ids(list(state["docs"]))
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        # A whole file built elsewhere (the refresh's runner) supersedes a delta still
        # waiting for canopy-web to build it: its builder, if one is running, finds the
        # project moved on and stops, and the served version is never touched by it.
        KbVersion.objects.filter(project=project, status__in=_LIVE).update(
            status=KbVersion.FAILED, error="superseded by a whole-file publish", finished_at=timezone.now())
        previous = head_state(project) if state is not None and project.kb_version_id else None
        head = project.kb_version
        project = kb_store.publish(project, key, sha256)
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.FULL, status=KbVersion.READY,
            base=head, state_sha=kb_store.put_blob(project, state) if state is not None else "",
            kb_key=project.kb_key, kb_sha256=project.kb_sha256, kb_bytes=project.kb_bytes,
            uploaded_bytes=project.kb_bytes or 0, published_by=published_by,
            started_at=timezone.now(), finished_at=timezone.now())
        if changes:
            d = {k: [str(i) for i in changes.get(k) or []] for k in ("added", "changed", "removed")}
            _log_ids(version, d, {str(i): r for i, r in (changes.get("rows") or {}).items() if isinstance(r, dict)})
        elif previous is not None:
            d = records.diff(previous, state)
            _log_ids(version, d)
        project.kb_version = version
        project.save(update_fields=["kb_version"])
        if project.kb_drive_replica:
            transaction.on_commit(lambda: _replicate_later(project.pk, version.pk))
    prune(project)
    return project, version


def _log_ids(version: KbVersion, d: dict, rows: dict | None = None) -> None:
    """The version's KbChange rows; `rows` gives (type, title, url, date) by id where known
    (a full publish and a rollback know only ids: `whats_new` fills the rest in)."""
    rows = rows or {}
    out = []
    for change, ids in ((KbChange.NEW, d["added"]), (KbChange.CHANGED, d["changed"]), (KbChange.REMOVED, d["removed"])):
        for iid in ids:
            r = rows.get(iid) or {}
            out.append(KbChange(version=version, item_id=iid[:300], change=change, type=str(r.get("type") or "")[:60],
                                title=str(r.get("title") or ""), url=str(r.get("url") or ""),
                                date=str(r.get("date") or "")[:40]))
    KbChange.objects.bulk_create(out, batch_size=2000)
    version.added, version.changed, version.removed = len(d["added"]), len(d["changed"]), len(d["removed"])
    version.save(update_fields=["added", "changed", "removed"])


def rollback(project, number: int, *, published_by: str = "") -> KbVersion:
    """Point the project at version `number`'s file again, as a new version of the log."""
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        target = KbVersion.objects.filter(project=project, number=number).first()
        if target is None:
            raise kb_store.KbPublishError(f"no version {number}")
        if target.status != KbVersion.READY or not target.kb_key:
            raise kb_store.KbPublishError(f"version {number} cannot be served again "
                                          f"({'its file was pruned' if target.status == KbVersion.READY else target.status})")
        head = project.kb_version
        if head is not None and head.kb_key == target.kb_key:
            raise kb_store.KbPublishError(f"version {number} is what the project serves already")
        if KbVersion.objects.filter(project=project, status__in=_LIVE).exists():
            raise KbConflict("a version is still being built; wait for it, then roll back")
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.ROLLBACK, status=KbVersion.READY,
            base=head, rollback_of=target, state_sha=target.state_sha, kb_key=target.kb_key,
            kb_sha256=target.kb_sha256, kb_bytes=target.kb_bytes, summary=target.summary,
            published_by=published_by, started_at=timezone.now(), finished_at=timezone.now())
        if head is not None and head.state_sha and target.state_sha:
            _log_ids(version, records.diff(kb_store.get_blob(project, head.state_sha),
                                           kb_store.get_blob(project, target.state_sha)))
        kb_store.point(project, target.kb_key, target.kb_sha256, target.kb_bytes or 0, version)
    return version


def prune(project) -> list[str]:
    """Delete the files of versions beyond the newest KEEP_FILES ready ones (and never
    the head's). Their log entries stay."""
    ready = list(KbVersion.objects.filter(project=project, status=KbVersion.READY).exclude(kb_key="")
                 .order_by("-number"))
    keep = {v.kb_key for v in ready[:KEEP_FILES]}
    if project.kb_key:
        keep.add(project.kb_key)
    gone = sorted({v.kb_key for v in ready[KEEP_FILES:]} - keep)
    for key in gone:
        kb_store.delete_object(key)
    if gone:
        KbVersion.objects.filter(project=project, kb_key__in=gone).update(kb_key="")
    return gone


def whats_new(project, kb, since: str = "", limit: int = 100) -> dict:
    """The `whats_new` tool from the version log: what each version since `since` added,
    changed and removed, newest first. History from before the log (carried in the
    file the first logged version published) follows it."""
    versions = list(KbVersion.objects.filter(project=project, status=KbVersion.READY).order_by("-number"))
    if not versions:
        return kb.whats_new(since, limit)
    first = versions[-1]
    since_ok = [v for v in versions if not since or (v.finished_at and v.finished_at.isoformat() >= since)]
    changes = []
    for row in (KbChange.objects.filter(version__in=since_ok).select_related("version")
                .order_by("-version__number", "change", "item_id")[:limit]):
        r = {"at": row.version.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "version": row.version.number,
             "item_id": row.item_id, "change": row.change, "type": row.type, "title": row.title,
             "url": row.url, "date": row.date}
        if not r["title"]:
            it = kb.item_row(row.item_id)
            if it is not None:
                r.update(type=it["type"], title=it["title"], url=it["url"], date=it["date"])
        changes.append(r)
    log_rows = [{"version": v.number, "kind": v.kind, "at": v.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "added": v.added, "changed": v.changed, "removed": v.removed,
                 **({"rollback_to": v.rollback_of.number} if v.rollback_of_id else {})} for v in since_ok]
    out = {"version": versions[0].number, "versions": log_rows, "changes": changes, "note": ""}
    first_at = first.finished_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if len(changes) < limit and (not since or since < first_at):
        older = [c for c in kb.whats_new(since, limit - len(changes))["changes"] if (c.get("at") or "") < first_at]
        if older:
            out["changes"] += older
            out["note"] = f"changes before version {first.number} ({first_at[:10]}) are from the file's own history"
    return out


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
            version.replica = f"drive:{kb_drive.write_replica(project, gz)}"
    except Exception as exc:  # noqa: BLE001 - the replica is a convenience; the version stands
        log.warning("kb replica for project %s failed: %s", project_pk, exc)
        version = KbVersion.objects.filter(pk=version_pk).first()
        if version is None:
            return
        version.replica = f"failed: {exc}"[:300]
    finally:
        close_old_connections()
    version.save(update_fields=["replica"])
