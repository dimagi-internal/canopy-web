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


# A narrative's stored `title` is derived from the opening of its story, so on a
# card it repeats the lede verbatim — the heading was a truncated copy of the
# paragraph directly beneath it. The release page already solved this with
# _humanize_slug + _lede_from_story; reuse them rather than inventing a third
# treatment. A genuinely short title (a real one, should narratives ever carry
# one) is still preferred.
_MAX_CARD_TITLE = 70


def _card_title(narrative_slug: str, stored_title: str | None) -> str:
    title = (stored_title or "").strip()
    if title and len(title) <= _MAX_CARD_TITLE:
        return title
    return aggregate._humanize_slug(narrative_slug)


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
        "title": _card_title(entry.narrative_slug, current.get("title") or narrative.get("title")),
        "lede": aggregate._lede_from_story(story, None) or story,
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


def resolve_narrative(board: Storyboard, narrative_slug: str) -> dict | None:
    """One narrative on this board, as the reviewer surface reads it.

    Returns the CURRENT version's narration plus the PREVIOUS one, so the page
    can show a before/after only where something actually changed. Returns None
    when the narrative is not on this board — one board's link must never be a
    read handle for a narrative it does not contain.
    """
    from apps.storyboards.models import Entry

    on_board = Entry.objects.filter(
        act__storyboard=board, narrative_slug=narrative_slug
    ).exists()
    if not on_board:
        return None

    narrative = aggregate.build_narrative(narrative_slug, {board.workspace_id})
    if narrative is None:
        return None

    # versions[] is newest-first from the aggregator.
    versions = [v for v in (narrative.get("versions") or []) if v.get("narration")]
    current = versions[0] if versions else None
    previous = versions[1] if len(versions) > 1 else None

    def _narration(v):
        return [
            {
                "id": str(n.get("id") or ""),
                "title": n.get("title") or "",
                "text": n.get("text") or "",
            }
            for n in (v.get("narration") or [])
        ]

    return {
        "narrative_slug": narrative_slug,
        "title": (current or {}).get("title") or narrative.get("title") or narrative_slug,
        "story": narrative.get("story") or "",
        "version": (current or {}).get("version"),
        "previous_version": (previous or {}).get("version"),
        "narration": _narration(current or {}),
        "previous_narration": _narration(previous or {}),
        "storyboard_slug": board.slug,
        "storyboard_title": board.title,
        "capability": board.capability,
    }
