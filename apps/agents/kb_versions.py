"""A workspace project's knowledge base as a version log: publish the difference.

A publish names a STATE — every document record's and corpus part's sha256
(`canopy_kb.records.state`). Against the head's state the server knows exactly what
changed, so a steward:

1. `plan(state)` — what changed since the head, and which records and parts to send.
   An unchanged state is `unchanged`: nothing is sent, no version is made.
2. uploads those (`kb_store.blobs_upload`: presigned, content-addressed, only what the
   store lacks), then
3. `commit(state, base)` — a DELTA version, pending until the index job
   (`kb_index`) applies it to the base's kb.sqlite and points the project at the
   result. The published version is untouched until then, and if the job fails.

A knowledge base's FIRST version is a delta from nothing: the index job builds it from
its records, so no machine builds kb.sqlite. A FULL publish (`publish_full`, the
presigned whole file) is the fallback; it carries its state when the steward sends
one, so the next publish can be a delta. A commit may carry the refresh's curation
state (`kb_store.save_curation`), saved with the version or refused with it. `rollback` points the project at an earlier version's file. Each version
lists what it added, changed and removed (`KbChange`) — `whats_new` reads that log.

Only the newest `KEEP_FILES` versions keep their kb.sqlite.gz (`prune`); older ones
stay in the log and can no longer be rolled back to.
"""
from __future__ import annotations

import logging

from canopy_kb import records
from django.db import transaction
from django.utils import timezone

from . import kb_index, kb_store
from .models import Agent, AgentProject, KbChange, KbVersion

log = logging.getLogger(__name__)

KEEP_FILES = 5
#: A version still on its way: one at a time per project.
_LIVE =(KbVersion.PENDING, KbVersion.BUILDING)
#: Records a commit checks are stored before it answers (a first publish of a large
#: knowledge base changes every record: ~11,000 HEADs would outlast the request).
CHECK_MAX = 1000
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


def commit(project, state: dict, base: int | None, *, published_by: str = "", uploaded_bytes: int = 0,
           check_ids=None, curation: dict | None = None) -> tuple[KbVersion | None, bool]:
    """A pending DELTA version from `base` to `state`, or (head, False) when nothing
    changed. `check_ids(ids)` vets the ids a source admits (a Drive source's approvals).
    `curation` ({sha, base}) is saved in the same transaction, changed or not, so a
    refresh's state and the version it published never disagree."""
    _state_ok(state)
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        head = project.kb_version
        if (head.number if head else None) != base:
            raise KbConflict(f"the knowledge base is at version {head.number if head else 'none'}, not {base}: plan again")
        if KbVersion.objects.filter(project=project, status__in=_LIVE).exists():
            raise KbConflict("a version is still being built; wait for it, then plan again")
        current = head_state(project)
        if current is None:
            raise kb_store.KbPublishError("publish the whole file once first (the head carries no state)")
        d = records.diff(current, state)
        if not (d["added"] or d["changed"] or d["removed"] or d["parts"]):
            if curation:
                kb_store.save_curation(project, curation.get("sha") or "", curation.get("base"),
                                       saved_by=published_by, version=base)
            return head, False
        if check_ids is not None:
            check_ids(d["added"] + d["changed"])
        need = {state["docs"][i] for i in d["added"] + d["changed"]} | {state["parts"][n] for n in d["parts"]}
        # Checked here up to CHECK_MAX (a HEAD each, inside this request); past that a
        # sample, and the index job reads every record anyway: one missing fails the
        # version, never the version readers see.
        check = need if len(need) <= CHECK_MAX else set(sorted(need)[:CHECK_MAX])
        missing = kb_store.missing_blobs(project, check)
        if missing:
            raise kb_store.KbPublishError(f"{len(missing)} records were not uploaded (first: {sorted(missing)[0]})")
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.DELTA, base=head,
            state_sha=kb_store.put_blob(project, state), added=len(d["added"]), changed=len(d["changed"]),
            removed=len(d["removed"]), uploaded_bytes=max(0, int(uploaded_bytes or 0)), published_by=published_by)
        if curation:  # a conflict here undoes the version too
            kb_store.save_curation(project, curation.get("sha") or "", curation.get("base"),
                                   saved_by=published_by, version=version.number)
        transaction.on_commit(kb_index.kick)
    return version, True


def publish_full(project, key: str, sha256: str, state: dict | None = None, *, published_by: str = ""):
    """The whole-file publish (`kb_store.publish`), recorded as a FULL version."""
    if state is not None:
        _state_ok(state)
    with transaction.atomic():
        project = AgentProject.objects.select_for_update().get(pk=project.pk)
        if KbVersion.objects.filter(project=project, status__in=_LIVE).exists():
            raise KbConflict("a version is still being built; wait for it, then publish")
        previous = head_state(project) if state is not None and project.kb_version_id else None
        head = project.kb_version
        project = kb_store.publish(project, key, sha256)
        version = KbVersion.objects.create(
            project=project, number=_next_number(project), kind=KbVersion.FULL, status=KbVersion.READY,
            base=head, state_sha=kb_store.put_blob(project, state) if state is not None else "",
            kb_key=project.kb_key, kb_sha256=project.kb_sha256, kb_bytes=project.kb_bytes,
            uploaded_bytes=project.kb_bytes or 0, published_by=published_by,
            started_at=timezone.now(), finished_at=timezone.now())
        if previous is not None:
            d = records.diff(previous, state)
            _log_ids(version, d)
        project.kb_version = version
        project.save(update_fields=["kb_version"])
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
