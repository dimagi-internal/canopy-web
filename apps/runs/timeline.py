"""Timeline source for DDD: rendered runs + narrative-review checkpoints.

Two ``kind``s under the ``ddd`` subsystem:

- ``run`` — a render (a ``run_id`` with artifacts). Sorted by the latest
  artifact/review timestamp in the run; links to the run package.
- ``narrative_review`` — a story-bearing review version (the narrative-agreement
  gate). Sorted by ``resolved_at`` (when decided) else ``created_at``; links to
  the review surface.

Reuses the read-time join helpers in :mod:`apps.runs.aggregate` so this stays
consistent with the /ddd views.
"""
from __future__ import annotations

import datetime as dt

from apps.reviews.models import ReviewRequest
from apps.walkthroughs.models import Walkthrough
from apps.workspaces import services as wsvc

from . import aggregate as agg


def _run_stamp(run_id: str) -> str:
    """'microplans-2026-06-02-001' -> '2026-06-02-001' (falls back to the id)."""
    import re

    m = re.search(r"(\d{4}-\d{2}-\d{2}-\d+)$", run_id or "")
    return m.group(1) if m else (run_id or "")


def recent_events(
    *,
    limit: int,
    before: dt.datetime | None,
    user,
    workspace_slugs: set[str] | None = None,
) -> list:
    from apps.timeline.types import ActivityEvent, actor_name

    # Full rollup over both tables, exactly like apps.runs.aggregate (the /ddd
    # views load the same way). A run's timeline timestamp is max(artifact,
    # review) so it can't be pushed to a per-row `before` filter without
    # splitting a run across the boundary; we filter run events by `before` in
    # Python below. Bounded by the team's DDD history (tens–hundreds of runs).
    #
    # ``workspace_slugs`` (offered by the timeline aggregator when the caller is
    # workspace-scoped) narrows both tables to the caller's tenants; ``None`` is a
    # no-op so the source stays usable unscoped.
    wts = list(
        agg._scope(
            Walkthrough.objects.exclude(run_id__isnull=True).exclude(run_id=""),
            workspace_slugs,
        ).select_related("owner")
    )
    revs = list(agg._scope(ReviewRequest.objects.all(), workspace_slugs))
    feature_map = agg._narrative_slug_map(wts)

    # Latest narrative-version review per narrative drives the run title. Gated on
    # _is_narrative_version (not _review_has_narrative) so a product_findings /
    # external_release review can't hijack the run title — same guard the /ddd
    # views use (aggregate._NON_NARRATIVE_GATES).
    title_by_narrative: dict[str, tuple[dt.datetime, str | None]] = {}
    for r in revs:
        # A review with no workspace has no address to link to.
        if not agg._is_narrative_version(r) or r.workspace_id is None:
            continue
        slug = agg.narrative_for_run_id(r.run_id, feature_map)
        prev = title_by_narrative.get(slug)
        if prev is None or r.created_at > prev[0]:
            title_by_narrative[slug] = (r.created_at, agg._title_from_review(r))

    wts_by_run: dict[str, list[Walkthrough]] = {}
    for w in wts:
        wts_by_run.setdefault(w.run_id, []).append(w)
    revs_by_run: dict[str, list[ReviewRequest]] = {}
    for r in revs:
        revs_by_run.setdefault(r.run_id, []).append(r)

    events: list[ActivityEvent] = []

    # Run events — one per run_id that has artifacts.
    for run_id, run_wts in wts_by_run.items():
        run_revs = revs_by_run.get(run_id, [])
        timestamps = [w.created_at for w in run_wts] + [r.created_at for r in run_revs]
        latest = max(timestamps)
        narrative = agg.narrative_of_walkthrough(run_wts[0])
        title = (title_by_narrative.get(narrative) or (None, None))[1] or narrative
        project_slug = next((w.project_slug for w in run_wts if w.project_slug), None)
        # The run's page, under the workspace its artifacts live in.
        href = next(
            (h for w in run_wts if (h := wsvc.scoped_path_or_none(w.workspace_id, f"/ddd/{narrative}/{run_id}"))),
            None,
        )
        if href is None:
            continue
        events.append(
            ActivityEvent(
                subsystem="ddd",
                kind="run",
                at=latest,
                title=f"Run · {title}",
                summary=_run_stamp(run_id),
                project_slug=project_slug,
                actor=None,
                href=href,
                id=f"run:{run_id}",
                icon="video" if any(agg._is_video(w) for w in run_wts) else "deck",
            )
        )

    # Narrative-version review events.
    for r in revs:
        if not agg._is_narrative_version(r):
            continue
        at = r.resolved_at or r.created_at
        narrative = agg.narrative_of_review(r)
        label = agg._title_from_review(r) or narrative
        events.append(
            ActivityEvent(
                subsystem="ddd",
                kind="narrative_review",
                at=at,
                title=f"Narrative v{r.version} · {label}",
                summary=f"{r.gate} · {r.status}",
                project_slug=None,
                actor=actor_name(r.owner),
                href=wsvc.scoped_path(r.workspace_id, f"/review/{r.id}"),
                id=f"review:{r.id}",
                icon="narrative",
            )
        )

    # Return candidates (newest `limit` strictly-older + all cursor-instant ties);
    # sources.gather applies the exact (at, id) cursor and the final slice.
    if before is None:
        events.sort(key=lambda e: (e.at, e.id), reverse=True)
        return events[:limit]
    older = sorted(
        (e for e in events if e.at < before),
        key=lambda e: (e.at, e.id),
        reverse=True,
    )
    ties = [e for e in events if e.at == before]
    return older[:limit] + ties
