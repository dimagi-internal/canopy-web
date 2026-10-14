"""Cascade deletes for the DDD aggregation surface.

A *narrative*/*version*/*run* is not a table — it's a grouping over
``ReviewRequest`` + ``Walkthrough`` rows (see ``aggregate.py``). Deleting one
therefore means deleting every row that rolls up under it, and best-effort
removing each walkthrough's rendered file from Drive (same cleanup the
single-walkthrough delete does in ``apps/walkthroughs/api.py``).

All three helpers return a small counts dict, or ``None`` when nothing matched
(the API layer turns ``None`` into a 404).
"""
from __future__ import annotations

import logging

from apps.reviews.models import ReviewRequest
from apps.walkthroughs import storage
from apps.walkthroughs.drive_client import DriveNotConfigured
from apps.walkthroughs.models import NarrativeSource, Walkthrough

from .aggregate import (
    _is_narrative_version,
    _scope,
    build_narrative,
    narrative_of_review,
    narrative_of_walkthrough,
)

logger = logging.getLogger(__name__)


def _drop_walkthrough(w: Walkthrough) -> None:
    """Best-effort Drive cleanup, then delete the row.

    Mirrors ``delete_walkthrough`` in ``apps/walkthroughs/api.py``: a missing or
    unconfigured Drive client means there's no orphan to clean up, so we still
    drop the row. Any other Drive error is logged but never blocks the delete —
    a stuck Drive file is better than a row we can't remove.
    """
    if w.drive_file_id:
        try:
            storage.delete_stored(file_id=w.drive_file_id, folder_id=w.drive_folder_id)
        except DriveNotConfigured:
            logger.warning(
                "ddd delete: drive not configured; dropping walkthrough %s without cleanup",
                w.id,
            )
        except Exception:  # noqa: BLE001 — never let Drive block the row delete
            logger.exception(
                "ddd delete: drive cleanup failed for walkthrough %s; dropping row anyway",
                w.id,
            )
    w.delete()


def _drop_source(src: NarrativeSource) -> None:
    from .sources import drop  # sources imports aggregate, as this module does

    drop(src)


def delete_run(run_id: str, workspace_slugs: set[str] | None = None) -> dict | None:
    """Delete a single run: every Walkthrough + ReviewRequest sharing ``run_id``.

    Scoped to ``workspace_slugs`` so a member of workspace A can't delete
    workspace B's run (a cross-tenant run_id resolves to nothing → 404).
    """
    wts = list(_scope(Walkthrough.objects.filter(run_id=run_id), workspace_slugs))
    revs = list(_scope(ReviewRequest.objects.filter(run_id=run_id), workspace_slugs))
    if not wts and not revs:
        return None
    for w in wts:
        _drop_walkthrough(w)
    n_rev = len(revs)
    for r in revs:
        r.delete()
    return {"run_id": run_id, "walkthroughs": len(wts), "reviews": n_rev}


def delete_version(
    slug: str, version: int, workspace_slugs: set[str] | None = None
) -> dict | None:
    """Delete one narrative version and the runs nested under it.

    Uses the same grouping the narrative page shows (``build_narrative``) so the
    blast radius matches exactly what the user saw under that version: its runs
    plus the version's own story review row(s).
    """
    data = build_narrative(slug, workspace_slugs)
    if data is None:
        return None
    vp = next((v for v in data["versions"] if v.get("version") == version), None)
    if vp is None:
        return None

    runs = 0
    for r in vp.get("runs", []):
        if delete_run(r["run_id"], workspace_slugs) is not None:
            runs += 1

    # The version's story review(s) — these are not "runs", they ARE the version.
    version_revs = [
        r
        for r in _scope(ReviewRequest.objects.all(), workspace_slugs)
        if narrative_of_review(r) == slug
        and _is_narrative_version(r)
        and r.version == version
    ]
    # The version's iteration sources go with it: they were made for this version.
    srcs = list(
        _scope(
            NarrativeSource.objects.filter(
                narrative_slug=slug, narrative_review_id__in=[r.id for r in version_revs]
            ),
            workspace_slugs,
        )
    )
    for src in srcs:
        _drop_source(src)
    for r in version_revs:
        r.delete()

    return {"slug": slug, "version": version, "runs": runs, "reviews": len(version_revs)}


def delete_narrative(
    slug: str, workspace_slugs: set[str] | None = None
) -> dict | None:
    """Delete an entire narrative: every row (all versions + all runs) for ``slug``."""
    wts = [
        w
        for w in _scope(Walkthrough.objects.all(), workspace_slugs)
        if narrative_of_walkthrough(w) == slug
    ]
    revs = [
        r
        for r in _scope(ReviewRequest.objects.all(), workspace_slugs)
        if narrative_of_review(r) == slug
    ]
    srcs = list(_scope(NarrativeSource.objects.filter(narrative_slug=slug), workspace_slugs))
    if not wts and not revs and not srcs:
        return None
    for w in wts:
        _drop_walkthrough(w)
    for src in srcs:
        _drop_source(src)
    n_rev = len(revs)
    for r in revs:
        r.delete()
    return {"slug": slug, "walkthroughs": len(wts), "reviews": n_rev}
