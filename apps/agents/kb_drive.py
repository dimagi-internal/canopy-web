"""A knowledge base's Drive source: nothing is read until the owner approves it.

The steward's CLI (`canopy kb drive inventory`) lists the source's folders as
METADATA only — names, types, sizes, owners, who can open each, modified times — and
posts it here (`ingest`). Nothing is opened. canopy-web then:

* flags what looks sensitive on names alone (`canopy_kb.sensitive.name_flags`: raw
  data / consent / patient / line list …, an owner outside the organisation);
* skips a file shared more narrowly than its folder (some of the folder's readers
  could not open it), and lists it as skipped for the owner;
* keeps each file's decision across refreshes. A file that appears under an approved
  folder later is admitted by that folder's `auto_approve` rule, else it waits as
  pending — the "new since approval" queue on the owner's review page.

Only the project's OWNER decides (`may_decide`): `owner_user`, whoever that is — never
the steward agent, unless the agent's login is the owner. The steward then reads only
`readable` files (`canopy kb drive sync`), checks each spreadsheet's column headers
(`canopy_kb.sensitive.header_flags`) and HOLDS one that names or identifies people
(`hold`), and indexes the rest. A publish naming a Drive document that is not
`indexable` is refused (`check_ids`), so a slip in a curator's pipeline cannot put an
unapproved file into the index.

A KB with a Drive source is queryable only by its owner, its steward and the people
the owner names (`may_read`; `AgentProject.kb_readers`) — not by every member of the
workspace, as a KB of public material is.
"""
from __future__ import annotations

import json
from pathlib import Path

from canopy_kb.sensitive import name_flags
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import kb_store
from .models import KbDriveFile, KbSource

#: A Drive document's id in the KB: `drive:<file id>`.
DOC_PREFIX = "drive:"
#: Owners at these domains are inside the organisation (else a name flag).
INTERNAL_DOMAINS = ("dimagi.com", "dimagi-ai.com")
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveSourceError(ValueError):
    pass


def internal_domains() -> tuple[str, ...]:
    return tuple(getattr(settings, "CANOPY_KB_INTERNAL_DOMAINS", None) or INTERNAL_DOMAINS)


def has_drive_source(project) -> bool:
    return KbSource.objects.filter(project=project, kind=KbSource.DRIVE).exists()


def may_decide(user, project) -> bool:
    """Only the project's owner approves what of their files is read."""
    return bool(user and user.is_authenticated and project.owner_user_id and project.owner_user_id == user.pk)


def may_read(user, project) -> bool:
    """Who may query the KB. One with a Drive source: its owner, its steward (and the
    steward's admins) and the readers the owner named; else no narrowing here."""
    if not has_drive_source(project):
        return True
    if user is None or not user.is_authenticated:
        return False
    lead = project.agent
    if project.owner_user_id == user.pk or (lead is not None and (lead.is_own_login(user) or lead.is_admin(user))):
        return True
    return project.kb_readers.filter(pk=user.pk).exists()


# ---- the inventory ----

def _readers(row: dict) -> set[str]:
    return {str(r) for r in row.get("readers") or [] if r}


def ingest(source: KbSource, files: list[dict], page_token: str = "") -> dict:
    """Record the steward's metadata listing of the source's folders. Returns counts:
    new files, the ones queued for a decision, auto-approved, gone, skipped, flagged."""
    if source.kind != KbSource.DRIVE:
        raise DriveSourceError("only a Drive source has an inventory")
    roots = [str(f) for f in (source.config or {}).get("folders") or []]
    by_id = {str(f["id"]): f for f in files if f.get("id")}
    missing_roots = [r for r in roots if r not in by_id]
    if missing_roots:
        raise DriveSourceError(f"the listing must include each source folder itself (missing {missing_roots[0]})")

    def chain(fid: str) -> list[dict]:
        """The file's folders, from its source root down (the file not included)."""
        out, seen, cur = [], {fid}, by_id[fid]
        while str(cur["id"]) not in roots and cur.get("parent") in by_id and cur["parent"] not in seen:
            seen.add(cur["parent"])
            cur = by_id[cur["parent"]]
            out.append(cur)
        return list(reversed(out))

    now = timezone.now()
    counts = {"files": 0, "folders": 0, "new": 0, "queued": 0, "auto_approved": 0, "gone": 0, "skipped": 0,
              "flagged": 0, "changed": 0}
    with transaction.atomic():
        existing = {r.file_id: r for r in KbDriveFile.objects.select_for_update().filter(source=source)}
        order = sorted(by_id, key=lambda fid: len(chain(fid)))  # folders before what they hold
        for fid in order:
            f = by_id[fid]
            folders = chain(fid)
            is_folder = (f.get("mime_type") or "") == FOLDER_MIME
            counts["folders" if is_folder else "files"] += 1
            parent = by_id.get(f.get("parent") or "")
            skip = ""
            # No readers listed means Drive did not show us its sharing (not ours to see), not
            # that nobody can open it: only a listed, smaller set is narrower.
            if parent is not None and fid not in roots and _readers(f):
                lost = _readers(parent) - _readers(f)
                if lost:
                    skip = f"shared more narrowly than its folder ({len(lost)} of the folder's readers cannot open it)"
            row = existing.get(fid)
            fields = dict(
                parent_id=str(f.get("parent") or "")[:128], name=str(f.get("name") or "")[:500],
                path=" / ".join(str(p.get("name") or "") for p in folders), mime_type=str(f.get("mime_type") or "")[:200],
                is_folder=is_folder, size=int(f["size"]) if str(f.get("size") or "").isdigit() else None,
                owner=str(f.get("owner") or "")[:254], modified_time=str(f.get("modified_time") or "")[:40],
                md5=str(f.get("md5") or "")[:64], drive_version=str(f.get("version") or "")[:40],
                web_url=str(f.get("web_url") or "")[:500], readers=sorted(_readers(f)), skip_reason=skip,
                flags=name_flags(str(f.get("name") or ""), [str(p.get("name") or "") for p in folders],
                                 str(f.get("owner") or ""), internal_domains()),
                present=True)
            if row is None:
                row = KbDriveFile(source=source, file_id=fid, **fields)
                _first_decision(row, [existing.get(str(p["id"])) for p in folders] + [existing.get(row.parent_id)])
                row.save()
                existing[fid] = row
                counts["new"] += 1
                if not is_folder:
                    if row.decision == KbDriveFile.APPROVED:
                        counts["auto_approved"] += 1
                    elif row.decision == KbDriveFile.PENDING and _under_approved(row, existing):
                        counts["queued"] += 1
            else:
                if (row.md5, row.drive_version) != (fields["md5"], fields["drive_version"]) and not is_folder:
                    counts["changed"] += 1
                    fields.update(held_headers=[], hold_released=False)  # new content: checked again
                for k, v in fields.items():
                    setattr(row, k, v)
                row.save()
            counts["skipped"] += bool(skip and not is_folder)
            counts["flagged"] += bool(row.flags)
        gone = [r for fid, r in existing.items() if fid not in by_id and r.present]
        for r in gone:
            r.present = False
            r.save(update_fields=["present", "updated_at"])
        counts["gone"] = sum(1 for r in gone if not r.is_folder)
        source.page_token = (page_token or source.page_token)[:200]
        source.inventoried_at = now
        source.save(update_fields=["page_token", "inventoried_at", "updated_at"])
    return counts


def _first_decision(row: KbDriveFile, ancestors: list[KbDriveFile | None]) -> None:
    """A file new to the inventory: excluded under an excluded folder; admitted under an
    approved folder whose rule says so; otherwise it waits for the owner."""
    folders = [a for a in ancestors if a is not None]
    if any(a.decision == KbDriveFile.EXCLUDED for a in folders):
        row.decision = KbDriveFile.EXCLUDED
        return
    nearest = next((a for a in reversed(folders) if a.decision == KbDriveFile.APPROVED), None)
    if nearest is not None and nearest.auto_approve and not row.flags:
        row.decision, row.decided_at = KbDriveFile.APPROVED, timezone.now()


def _under_approved(row: KbDriveFile, rows: dict[str, KbDriveFile]) -> bool:
    cur, seen = rows.get(row.parent_id), set()
    while cur is not None and cur.file_id not in seen:
        if cur.decision == KbDriveFile.APPROVED:
            return True
        seen.add(cur.file_id)
        cur = rows.get(cur.parent_id)
    return False


def queue(source: KbSource) -> list[KbDriveFile]:
    """New since approval: pending files that appeared under an approved folder."""
    rows = {r.file_id: r for r in source.drive_files.all()}
    return [r for r in rows.values() if not r.is_folder and r.present and r.decision == KbDriveFile.PENDING
            and _under_approved(r, rows)]


def readable(source: KbSource):
    """The files whose contents the steward may read."""
    return source.drive_files.filter(is_folder=False, present=True, decision=KbDriveFile.APPROVED, skip_reason="")


def is_indexable(row: KbDriveFile) -> bool:
    return (not row.is_folder and row.present and row.decision == KbDriveFile.APPROVED and not row.skip_reason
            and (not row.held_headers or row.hold_released))


# ---- the owner's decisions ----

def decide(source: KbSource, user, decisions: list[dict]) -> dict:
    """Apply the owner's switches. A folder's decision reaches everything under it that is
    not already decided the other way (an excluded file stays excluded when its folder
    is approved; excluding a folder excludes all of it)."""
    rows = {r.file_id: r for r in source.drive_files.all()}
    children: dict[str, list[KbDriveFile]] = {}
    for r in rows.values():
        children.setdefault(r.parent_id, []).append(r)
    now = timezone.now()
    changed: set[str] = set()

    def under(fid: str):
        stack = list(children.get(fid, []))
        while stack:
            r = stack.pop()
            yield r
            stack += children.get(r.file_id, [])

    with transaction.atomic():
        for d in decisions:
            row = rows.get(str(d.get("file_id") or ""))
            if row is None:
                raise DriveSourceError(f"no file {d.get('file_id')!r} in this source")
            new = d.get("decision")
            if new is not None:
                if new not in (KbDriveFile.APPROVED, KbDriveFile.EXCLUDED, KbDriveFile.PENDING):
                    raise DriveSourceError(f"decision must be approved, excluded or pending, not {new!r}")
                row.decision, row.decided_by, row.decided_at = new, user, now
                changed.add(row.file_id)
                if row.is_folder and new != KbDriveFile.PENDING:
                    for r in under(row.file_id):
                        if new == KbDriveFile.EXCLUDED or r.decision == KbDriveFile.PENDING:
                            r.decision, r.decided_by, r.decided_at = new, user, now
                            changed.add(r.file_id)
            if "auto_approve" in d and d["auto_approve"] is not None:
                if not row.is_folder:
                    raise DriveSourceError(f"{row.name}: auto-approve is a folder's rule")
                row.auto_approve = bool(d["auto_approve"])
                changed.add(row.file_id)
            if d.get("release_hold"):
                row.hold_released = True
                changed.add(row.file_id)
        for fid in changed:
            rows[fid].save()
    return {"changed": len(changed)}


def hold(source: KbSource, file_id: str, headers: list[str]) -> KbDriveFile:
    """The steward found personal-data headers in an approved spreadsheet: hold it."""
    row = source.drive_files.filter(file_id=file_id).first()
    if row is None:
        raise DriveSourceError(f"no file {file_id!r} in this source")
    row.held_headers = [str(h)[:200] for h in headers][:100]
    row.hold_released = False if row.held_headers else row.hold_released
    row.save(update_fields=["held_headers", "hold_released", "updated_at"])
    return row


def check_ids(project):
    """A publish's vetting of the Drive documents its file holds (`kb_versions.publish_full`
    reads their ids from the uploaded file itself), or None for a knowledge base with no
    Drive source (whose `drive:` ids, such as files a
    publisher links on Drive, are not a folder owner's to approve)."""
    if not KbSource.objects.filter(project=project, kind=KbSource.DRIVE).exists():
        return None

    def check(ids: list[str]) -> None:
        drive_ids = [i[len(DOC_PREFIX):] for i in ids if i.startswith(DOC_PREFIX)]
        if not drive_ids:
            return
        rows = {r.file_id: r for r in KbDriveFile.objects.filter(source__project=project, file_id__in=drive_ids)}
        bad = [fid for fid in drive_ids if fid not in rows or not is_indexable(rows[fid])]
        if bad:
            raise kb_store.KbPublishError(f"{len(bad)} Drive documents are not approved for indexing "
                                          f"(first: {DOC_PREFIX}{bad[0]}); see the project's review page")
    return check


# ---- the Drive replica (off by default) ----

def write_replica(project, gz: Path) -> str:
    """Write a version's kb.sqlite.gz into the project's Drive folder with canopy-web's
    service account (the folder must be shared with it), replacing the last one."""
    from google.oauth2 import service_account  # noqa: PLC0415 — the Google client loads only for a replica
    from googleapiclient.discovery import build  # noqa: PLC0415 — the Google client loads only for a replica
    from googleapiclient.http import MediaFileUpload  # noqa: PLC0415 — the Google client loads only for a replica

    if not project.drive_folder_id:
        raise DriveSourceError("the project has no Drive folder")
    raw = getattr(settings, "CANOPY_DRIVE_SA_KEY_JSON", "") or ""
    if not raw:
        raise DriveSourceError("no CANOPY_DRIVE_SA_KEY_JSON on this deployment")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(str(gz), mimetype="application/gzip", resumable=True)
    made = drive.files().create(body={"name": "kb.sqlite.gz", "parents": [project.drive_folder_id]},
                                media_body=media, fields="id", supportsAllDrives=True).execute()
    previous = project.kb_drive_replica_file_id
    project.kb_drive_replica_file_id = made["id"]
    project.save(update_fields=["kb_drive_replica_file_id"])
    if previous:
        try:
            drive.files().delete(fileId=previous, supportsAllDrives=True).execute()
        except Exception:  # noqa: BLE001 - a stale replica costs Drive space, not correctness
            pass
    return made["id"]
