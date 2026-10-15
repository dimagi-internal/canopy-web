"""A storyboard entry's cuts, as a reviewer judges them.

A ``style: recorded`` narrative is several short cuts, each its own video
(canopy-web#1288). The board showed one hero video per narrative, so a reviewer
of a set of demos could not see — let alone comment on — the cuts the set is
actually made of, and the per-cut facts a reviewer needs (how long, how many
words, what is on screen, why it matters to this audience, where the data comes
from) lived in a companion Google Doc.

This joins two sources, and invents nothing:

- the cut VIDEOS pinned to the version (``aggregate`` → ``current_version.cuts``),
  which carry the cut id, title, scene ids, playable URLs and measured length;
- the optional ``cuts[]`` the publisher sent in that version's request_json,
  keyed by the same id, carrying the words a person wrote about each cut.

Word count is counted from the narration the cut plays, never sent.
"""
from __future__ import annotations

from typing import Any, Callable


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def _int(v: Any) -> int | None:
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def word_count(text: str) -> int:
    return len((text or "").split())


def caveats(request_json: dict | None, narrative_slug: str) -> dict:
    """The narrative's own assumptions / divergences / open questions."""
    rj = request_json if isinstance(request_json, dict) else {}
    divergences = []
    for d in rj.get("divergences") or []:
        if isinstance(d, dict) and _s(d.get("script")):
            divergences.append(
                {
                    "script": _s(d.get("script")),
                    "data": _s(d.get("data")),
                    "resolution": _s(d.get("resolution")),
                    "narrative_slug": narrative_slug,
                }
            )
    return {
        "assumptions": _str_list(rj.get("assumptions")),
        "divergences": divergences,
        "open_questions": _str_list(rj.get("open_questions")),
    }


def entry_cuts(
    pinned_cuts: list[dict],
    request_json: dict | None,
    *,
    playable: Callable[[str | None], bool],
) -> list[dict]:
    """Every cut of a version, in running order.

    ``pinned_cuts`` is ``current_version["cuts"]`` (already in narration order).
    ``playable(url)`` says whether THIS reader may be handed a video URL; a URL
    they cannot stream renders a black box, so it is withheld instead.

    A cut the publisher declared in ``cuts[]`` that no video is pinned to yet
    is listed as not recorded, so it reads "not recorded yet" rather than
    silently missing. Order: the publisher's ``cuts[]`` order when it names
    every cut (that IS the running order they chose), else narration order.
    """
    rj = request_json if isinstance(request_json, dict) else {}
    narration = {
        str(n.get("id")): n for n in (rj.get("narration") or []) if isinstance(n, dict)
    }
    declared = [c for c in (rj.get("cuts") or []) if isinstance(c, dict) and _s(c.get("id"))]
    meta = {_s(c.get("id")): c for c in declared}

    def scenes_of(ids: list[str]) -> list[dict]:
        out = []
        for sid in ids:
            n = narration.get(str(sid))
            if n is not None:
                out.append(
                    {
                        "id": str(sid),
                        "title": _s(n.get("title")),
                        "text": _s(n.get("text")),
                    }
                )
        return out

    def build(cut_id: str, title: str, scene_ids: list[str], video: dict | None) -> dict:
        m = meta.get(cut_id, {})
        scenes = scenes_of(scene_ids)
        provenance = _s(m.get("provenance"))
        if not provenance:
            # The scenes already say where their data comes from.
            seen: list[str] = []
            for sid in scene_ids:
                p = _s((narration.get(str(sid)) or {}).get("provenance"))
                if p and p not in seen:
                    seen.append(p)
            provenance = "; ".join(seen)
        video_url = (video or {}).get("video_url")
        can_play = bool(video_url) and playable(video_url)
        final_title = _s(m.get("title")) or title or cut_id
        return {
            "cut_id": cut_id,
            "anchor_id": f"cut:{cut_id}",
            "title": final_title,
            "recorded": video is not None,
            "video_url": video_url if can_play else None,
            "video_viewer_url": (video or {}).get("video_viewer_url") if can_play else None,
            "poster_url": _s(m.get("poster_url")) or None,
            "duration_sec": (video or {}).get("duration_sec") or _int(m.get("duration_sec")),
            # The product screens the cut films. Plain URLs: shown even when
            # the video itself is withheld from this reader.
            "app_links": [
                {"label": _s(a.get("label")) or _s(a.get("url")), "url": _s(a.get("url"))}
                for a in ((video or {}).get("app_links") or [])
                if isinstance(a, dict) and _s(a.get("url"))
            ],
            "word_count": sum(word_count(s["text"]) for s in scenes),
            "scenes": scenes,
            "screen_notes": _s(m.get("screen_notes")),
            "audience_note": _s(m.get("audience_note")),
            "provenance": provenance,
            "optional": bool(m.get("optional")) or "optional" in final_title.lower(),
        }

    rows: dict[str, dict] = {}
    order: list[str] = []
    for v in pinned_cuts or []:
        cid = _s(v.get("cut_id"))
        if not cid or cid in rows:
            continue
        rows[cid] = build(cid, _s(v.get("title")), [str(x) for x in v.get("scene_ids") or []], v)
        order.append(cid)
    for c in declared:
        cid = _s(c.get("id"))
        if cid in rows:
            continue
        rows[cid] = build(cid, _s(c.get("title")), [str(x) for x in c.get("scene_ids") or []], None)
        order.append(cid)

    declared_ids = [_s(c.get("id")) for c in declared]
    if declared_ids and set(declared_ids) >= set(order):
        order = [cid for cid in declared_ids if cid in rows]
    return [rows[cid] for cid in order]
