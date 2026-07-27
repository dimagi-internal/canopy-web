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
from apps.storyboards.models import Entry, Storyboard


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


def _streamable_anonymously(video_url: str | None) -> bool:
    """Whether a reader with no login can actually play this.

    Not an inference: ``aggregate._content_url`` appends ``?t=<share_token>``
    exactly when the walkthrough is ``visibility=link``, and the stream 404s for
    an anonymous caller in every other case. So the token's presence IS the
    answer.
    """
    return bool(video_url) and "t=" in (video_url or "")


def _entry_payload(entry, workspace_slugs: set[str], *, is_member: bool) -> dict:
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

    # The board FOLLOWS the current release, so the next version's walkthrough
    # can land private under a link that has already been sent. Handing a reader
    # a <video> they cannot stream renders a black box with no explanation;
    # say the artifact is not shared instead.
    video_url = current.get("video_url")
    shared = is_member or _streamable_anonymously(video_url)

    return {
        "narrative_slug": entry.narrative_slug,
        "title": _card_title(entry.narrative_slug, current.get("title") or narrative.get("title")),
        "lede": aggregate._lede_from_story(story, None) or story,
        "version": current.get("version"),
        "video_url": video_url if shared else None,
        "video_viewer_url": current.get("video_viewer_url") if shared else None,
        "published": bool(current),
    }


def board_feedback(board: Storyboard):
    """Every note left on this board — the arc itself and the narratives on it.

    Members only, enforced by the caller. A reviewer must not read what other
    reviewers said: it biases the feedback you asked them for, and the rows
    carry names and email addresses that were given to us, not to each other.
    """
    from django.db.models import Q

    from apps.feedback.models import Feedback

    slugs = list(
        Entry.objects.filter(act__storyboard=board)
        .values_list("narrative_slug", flat=True)
        .distinct()
    )
    return Feedback.objects.filter(
        Q(target_kind="storyboard", target_ref=board.slug)
        | Q(target_kind="narrative", target_ref__in=slugs)
    ).order_by("-created_at")


def resolve_board(board: Storyboard, *, is_member: bool = False) -> dict:
    """The read model for the shared page: acts → entries → current release."""
    workspace_slugs = {board.workspace_id}

    acts = []
    for act in board.acts.all():
        acts.append(
            {
                # What an act-level note is ABOUT. Without it every act note
                # targets the whole board with a blank anchor, so three notes on
                # three acts arrive indistinguishable — and the reader's most
                # structural feedback ("act II doesn't follow from act I") is
                # exactly the kind that loses its meaning unanchored.
                # See Act.key for why this is not the row id.
                "anchor_id": act.anchor_id,
                "title": act.title,
                "prose": act.prose,
                "entries": [
                    _entry_payload(e, workspace_slugs, is_member=is_member)
                    for e in act.entries.all()
                ],
            }
        )

    return {
        "slug": board.slug,
        "title": board.title,
        "lede": board.lede,
        "capability": board.capability,
        # The page shows returning notes only to the people who sent the link.
        "is_member": is_member,
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

    # Same treatment as the cards: the stored title is derived from the opening
    # of the story, so using it verbatim makes the heading a truncated copy of
    # the text beneath it. And the STORY here is the whole narration
    # concatenated — printing it above a scene-by-scene breakdown makes the
    # reader read everything twice. One sentence is the hook; the scenes are the
    # substance.
    return {
        "narrative_slug": narrative_slug,
        "title": _card_title(narrative_slug, (current or {}).get("title") or narrative.get("title")),
        "story": aggregate._lede_from_story(narrative.get("story") or "", None) or "",
        "version": (current or {}).get("version"),
        "previous_version": (previous or {}).get("version"),
        "narration": _narration(current or {}),
        "previous_narration": _narration(previous or {}),
        "storyboard_slug": board.slug,
        "storyboard_title": board.title,
        "capability": board.capability,
    }
