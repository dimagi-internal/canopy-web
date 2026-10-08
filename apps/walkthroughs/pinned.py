"""The videos pinned to a narrative version: its hero, and one per cut.

A video walkthrough stamped with a version's review id (``narrative_review_id``)
belongs to that exact story version. An explainer narrative has one such video.
A ``style: recorded`` narrative (canopy#796) renders one mp4 per cut, each
uploaded with its recipe ``cut_id`` — so a version holds a video PER CUT, and the
narrative page and the review link both read them from here, so the two cannot
disagree about which video is which cut (canopy-web#1288).

The hero (the narrative-level video) is, in order:
  1. the latest pinned video that is NOT a cut — an explainer render;
  2. the latest cut uploaded as the hero (``role == hero_video``) — a chosen cut;
  3. the first cut, in narration order.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import Walkthrough


@dataclass
class PinnedVideos:
    hero: Walkthrough | None = None
    # One per cut id, latest upload wins, in narration order.
    cuts: list[Walkthrough] = field(default_factory=list)


def _narration_order(narration: Iterable[dict] | None) -> dict[str, int]:
    order: dict[str, int] = {}
    for i, item in enumerate(narration or []):
        sid = str((item or {}).get("id") or "").strip()
        if sid:
            order.setdefault(sid, i)
    return order


def resolve(videos: Iterable[Walkthrough], narration: Iterable[dict] | None = None) -> PinnedVideos:
    """Resolve one version's pinned videos, given oldest-first ``videos``.

    ``narration`` (the version's ``request_json.narration``) orders the cuts by
    where their first scene falls; a cut whose scenes are not in it sorts last,
    by upload order."""
    plain: Walkthrough | None = None
    by_cut: dict[str, Walkthrough] = {}
    for w in videos:  # ascending → latest wins
        if w.cut_id:
            by_cut[w.cut_id] = w
        else:
            plain = w

    order = _narration_order(narration)
    unplaced = len(order) + 1

    def _pos(w: Walkthrough) -> tuple[int, object]:
        positions = [order[s] for s in (w.cut_scene_ids or []) if s in order]
        return (min(positions) if positions else unplaced, w.created_at)

    cuts = sorted(by_cut.values(), key=_pos)
    hero = plain
    if hero is None:
        chosen = [w for w in cuts if w.role == Walkthrough.ROLE_HERO_VIDEO]
        if chosen:
            hero = max(chosen, key=lambda w: w.created_at)
        elif cuts:
            hero = cuts[0]
    return PinnedVideos(hero=hero, cuts=cuts)


def pinned_video_rows(review_ids: Iterable, qs=None) -> dict[str, list[Walkthrough]]:
    """Oldest-first pinned video rows per review id. ``qs`` lets a caller
    pre-scope the queryset to the workspaces it may read."""
    ids = list(review_ids)
    out: dict[str, list[Walkthrough]] = {}
    if not ids:
        return out
    base = qs if qs is not None else Walkthrough.objects.all()
    for w in base.filter(kind=Walkthrough.KIND_VIDEO, narrative_review_id__in=ids).order_by(
        "created_at"
    ):
        out.setdefault(str(w.narrative_review_id), []).append(w)
    return out


def cut_title(w: Walkthrough) -> str:
    """The cut's human label: the upload's title, else its id."""
    return (w.title or "").strip() or w.cut_id
