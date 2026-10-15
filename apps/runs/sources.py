"""A narrative's iteration sources: the files that let anyone keep iterating it.

The story lives on each version's ``ReviewRequest`` and the renders are
``Walkthrough`` rows, but a recorded narrative can't be re-recorded or re-cut
from those alone — that takes the recipe, the master recording and the capture
report. Those are ``NarrativeSource`` rows, pinned to the version they were made
for. This module is the one place that stores, lists and drops them; the API in
``apps/runs/api.py`` and the cascades in ``delete.py`` / ``transfer.py`` call it.
"""
from __future__ import annotations

import hashlib
import logging
import uuid

from apps.reviews.models import ReviewRequest
from apps.walkthroughs import storage
from apps.walkthroughs.drive_client import DriveNotConfigured
from apps.walkthroughs.models import NarrativeSource

from .aggregate import _is_narrative_version, _scope, narrative_of_review

logger = logging.getLogger(__name__)


def content_path(src: NarrativeSource) -> str:
    return (
        f"/api/w/{src.workspace_id}/ddd/narratives/{src.narrative_slug}"
        f"/sources/{src.id}/content"
    )


def version_review(
    slug: str, review_id: uuid.UUID, workspace_slugs: set[str] | None
) -> ReviewRequest | None:
    """The narrative version ``review_id`` of ``slug``, if it is one and is in scope."""
    r = _scope(ReviewRequest.objects.filter(id=review_id), workspace_slugs).first()
    if r is None or narrative_of_review(r) != slug or not _is_narrative_version(r):
        return None
    return r


def for_slug(slug: str, workspace_slugs: set[str] | None) -> list[NarrativeSource]:
    """Every source of ``slug`` in scope, newest first."""
    return list(_scope(NarrativeSource.objects.filter(narrative_slug=slug), workspace_slugs))


def latest_by_review(sources: list[NarrativeSource]) -> dict[str, list[NarrativeSource]]:
    """review id -> its newest source per role (``sources`` must be newest first)."""
    out: dict[str, dict[str, NarrativeSource]] = {}
    for s in sources:
        out.setdefault(str(s.narrative_review_id), {}).setdefault(s.role, s)
    return {k: list(v.values()) for k, v in out.items()}


def ref_payload(s: NarrativeSource) -> dict:
    return {
        "role": s.role,
        "filename": s.filename,
        "size_bytes": s.size_bytes,
        "sha256": s.sha256,
        "created_at": s.created_at,
    }


def detail_payload(s: NarrativeSource, versions: dict[str, int | None]) -> dict:
    return {
        **ref_payload(s),
        "id": s.id,
        "content_type": s.content_type,
        "review_id": s.narrative_review_id,
        "version": versions.get(str(s.narrative_review_id)),
        "workspace": str(s.workspace_id),
        "content_url": content_path(s),
    }


def store(
    *, review: ReviewRequest, slug: str, role: str, data: bytes, owner
) -> tuple[NarrativeSource, bool]:
    """Pin ``data`` to ``review`` as its ``role`` source. Returns (row, reused).

    Identical bytes already pinned to the same version and role are reused
    rather than uploaded again (a master is tens of MB and rarely changes
    between uploads). Otherwise the new file replaces the version's previous one
    for that role, after the new one is safely stored."""
    digest = hashlib.sha256(data).hexdigest()
    existing = NarrativeSource.objects.filter(
        workspace_id=review.workspace_id, narrative_review_id=review.id, role=role,
    )
    same = existing.filter(sha256=digest).first()
    if same is not None:
        return same, True

    filename, content_type = NarrativeSource.ROLES[role]
    src = NarrativeSource.objects.create(
        workspace_id=review.workspace_id,
        narrative_slug=slug,
        narrative_review_id=review.id,
        role=role,
        content_type=content_type,
        size_bytes=len(data),
        sha256=digest,
        owner=owner,
    )
    try:
        stored = storage.store_upload(
            walkthrough_id=str(src.id),
            filename=filename,
            content_type=content_type,
            data=data,
            folder=storage.NARRATIVE_SOURCES_FOLDER,
        )
    except Exception:
        src.delete()
        raise
    src.drive_file_id = stored.file_id
    src.drive_folder_id = stored.folder_id
    src.save(update_fields=["drive_file_id", "drive_folder_id"])

    for old in existing.exclude(pk=src.pk):
        drop(old)
    return src, False


def drop(src: NarrativeSource) -> None:
    """Best-effort Drive cleanup, then delete the row (never blocked by Drive)."""
    if src.drive_file_id:
        try:
            storage.delete_stored(file_id=src.drive_file_id, folder_id=src.drive_folder_id)
        except DriveNotConfigured:
            logger.warning("narrative source %s: drive not configured; dropping row", src.id)
        except Exception:  # noqa: BLE001 — a stuck Drive file beats an undeletable row
            logger.exception("narrative source %s: drive cleanup failed; dropping row", src.id)
    src.delete()
