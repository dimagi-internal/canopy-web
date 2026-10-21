"""One document once.

`collapse_duplicates`: identical texts at different URLs (a PDF linked from three pages
under three paths) become one item that lists its other URLs and every referrer.
`collapse_shared_tabs`: a spreadsheet tab copied verbatim into many workbooks keeps its
rows in one; every other copy becomes a one-line pointer. On GiveWell (2026-10-20) the
"Moral weights and discount rate" tab sat in 89 cost-effectiveness models: 171M of 1.1G
spreadsheet characters, which otherwise answer every search about that tab 89 times.
"""
from __future__ import annotations

import collections
import hashlib
import re
from typing import Callable

from canopy_kb.prep.raw import sha

#: A tab shorter than this is not worth a pointer.
SHARED_TAB_MIN = 2000
_TAB_SPLIT = re.compile(r"(?m)^(?=## Sheet: )")


def _shortest(it: dict):
    return len(it["url"])


def collapse_duplicates(items: list[dict], prefer: Callable[[dict], object] = _shortest) -> tuple[list[dict], int]:
    """Keep the item `prefer` ranks first (lowest) of each group of identical texts; list
    the others' urls (`meta.also_at`), ids (`meta.also_ids`) and referrers."""
    groups: dict[str, list] = collections.defaultdict(list)
    for it in items:
        groups[sha(re.sub(r"\W+", "", it["text"].lower())[:2_000_000])].append(it)
    final, dups, seen = [], 0, set()
    for g in groups.values():
        g.sort(key=prefer)
        keep = g[0]
        others = [x for x in g[1:] if x["id"] != keep["id"]]
        if others:
            dups += len(others)
            keep["meta"]["also_at"] = sorted({x["url"] for x in others} - {keep["url"]})[:20]
            keep["meta"]["also_ids"] = sorted({x["id"] for x in others})[:20]
            refs = set(keep["meta"].get("linked_from") or [])
            for x in others:
                refs.update(x["meta"].get("linked_from") or [])
            if refs:
                keep["meta"]["linked_from"] = sorted(refs)
        if keep["id"] in seen:
            continue
        seen.add(keep["id"])
        final.append(keep)
    return final, dups


def collapse_shared_tabs(items: list[dict], types: tuple[str, ...] = ("cea_model", "dataset")) -> int:
    """Each tab copied into several workbooks of `types` keeps its rows in the most
    linked-to; the other copies point there (`meta.shared_tabs_in`). Returns how many
    tabs were shared. Expects `## Sheet: <name>` sections (`xlsx.render`)."""
    def key(part: str) -> str | None:
        if not part.startswith("## Sheet:") or len(part) < SHARED_TAB_MIN:
            return None
        body = part.split("\n", 1)[1] if "\n" in part else ""
        return hashlib.md5(" ".join(body.split()).encode()).hexdigest()

    where: dict[str, set[int]] = collections.defaultdict(set)
    for n, it in enumerate(items):
        if it["type"] in types:
            for part in _TAB_SPLIT.split(it["text"]):
                k = key(part)
                if k:
                    where[k].add(n)
    home = {k: max(ns, key=lambda n: (len(items[n]["meta"].get("linked_from") or []), -len(items[n]["text"])))
            for k, ns in where.items() if len(ns) > 1}
    for n, it in enumerate(items):
        if it["type"] not in types:
            continue
        out, pointed = [], set()
        for part in _TAB_SPLIT.split(it["text"]):
            k = key(part)
            if k in home and home[k] != n:
                h = items[home[k]]
                name = part.split("\n", 1)[0][len("## Sheet: "):].strip()
                out.append(f"## Sheet: {name}\n(Identical to the tab \"{name}\" in {h['id']} \"{h['title']}\"; "
                           f"its rows are there.)\n\n")
                pointed.add(h["id"])
            else:
                out.append(part)
        if pointed:
            it["text"] = "".join(out)
            it["meta"]["shared_tabs_in"] = sorted(pointed)
    return len(home)
