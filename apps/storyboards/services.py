"""Resolving a storyboard into what the shared page renders.

The one rule here: **follow, don't freeze.** An entry names a narrative by slug
and resolves to that narrative's CURRENT version at read time, so an emailed
link never goes stale. ``Entry.pinned_run_id`` overrides that for the one case
that needs it (holding a known-good run while the narrative is mid-redraft).

This calls ``apps.runs.aggregate`` rather than re-deriving anything. A second
copy of release-building logic is exactly the drift this whole effort exists to
remove — and ``storyboards`` is product tier, so importing product is legal.
"""
from __future__ import annotations

from apps.runs import aggregate
from apps.storyboards.models import Storyboard


def _entry_payload(entry, workspace_slugs: set[str]) -> dict:
    """One entry, resolved to what the page shows for it.

    A narrative with nothing published yet resolves to a PLACEHOLDER rather than
    raising: a storyboard is often authored before its narratives are rendered,
    and an arc that 500s because act three has not been filmed yet would be
    useless exactly when you are building it.
    """
    narrative = aggregate.build_narrative(entry.narrative_slug, workspace_slugs)

    if narrative is None:
        return {
            "narrative_slug": entry.narrative_slug,
            "title": entry.narrative_slug,
            "lede": "",
            "version": None,
            "video_url": None,
            "video_viewer_url": None,
            "published": False,
        }

    current = narrative.get("current_version") or {}
    story = current.get("story") or narrative.get("story") or ""
    return {
        "narrative_slug": entry.narrative_slug,
        "title": current.get("title") or narrative.get("title") or entry.narrative_slug,
        "lede": story,
        "version": current.get("version"),
        "video_url": current.get("video_url"),
        "video_viewer_url": current.get("video_viewer_url"),
        "published": bool(current),
    }


def resolve_board(board: Storyboard) -> dict:
    """The read model for the shared page: acts → entries → current release."""
    workspace_slugs = {board.workspace_id}

    acts = []
    for act in board.acts.all():
        acts.append(
            {
                "title": act.title,
                "prose": act.prose,
                "entries": [
                    _entry_payload(e, workspace_slugs) for e in act.entries.all()
                ],
            }
        )

    return {
        "slug": board.slug,
        "title": board.title,
        "lede": board.lede,
        "capability": board.capability,
        "acts": acts,
    }
