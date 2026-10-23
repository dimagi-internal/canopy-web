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


def norm_key(text: str) -> str:
    """What two texts share when they say the same (case, spacing, punctuation aside)."""
    return sha(re.sub(r"\W+", "", text.lower())[:2_000_000])


def collapse_duplicates(items: list[dict], prefer: Callable[[dict], object] = _shortest,
                        key: Callable[[dict], str] | None = None) -> tuple[list[dict], int]:
    """Keep the item `prefer` ranks first (lowest) of each group of identical texts; list
    the others' urls (`meta.also_at`), ids (`meta.also_ids`) and referrers. `key` says
    which texts are identical without the text (a refresh's descriptors carry `norm`)."""
    key = key or (lambda it: norm_key(it["text"]))
    groups: dict[str, list] = collections.defaultdict(list)
    for it in items:
        groups[key(it)].append(it)
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


# -- the same, on a refresh's descriptors (no text in hand) ---------------------------------

def tab_keys(text: str) -> list[list]:
    """[name, key or None] for each `## Sheet:` part of a workbook's text, in order: what
    `shared_homes` needs to know of it without the text."""
    def key(part: str) -> str | None:
        if not part.startswith("## Sheet:") or len(part) < SHARED_TAB_MIN:
            return None
        body = part.split("\n", 1)[1] if "\n" in part else ""
        return hashlib.md5(" ".join(body.split()).encode()).hexdigest()

    out = []
    for part in _TAB_SPLIT.split(text):
        name = part.split("\n", 1)[0][len("## Sheet: "):].strip() if part.startswith("## Sheet:") else ""
        out.append([name, key(part)])
    return out


def shared_homes(items: list[dict], keep: dict[str, str] | None = None,
                 types: tuple[str, ...] = ("cea_model", "dataset")) -> dict[str, str]:
    """{tab key: the item id that keeps its rows} for tabs in several workbooks. Items carry
    `tabs` (`tab_keys`) and `chars`. A home in `keep` stays while it is still one of the
    copies, so a change in link counts does not move a tab's rows between 89 workbooks;
    a new group's home is the most linked-to copy, then the shortest, then the first id."""
    keep = keep or {}
    where: dict[str, list[dict]] = collections.defaultdict(list)
    for it in items:
        if it["type"] in types:
            for k in {k for _, k in it.get("tabs") or [] if k}:
                where[k].append(it)
    homes = {}
    for k, its in where.items():
        if len(its) < 2:
            continue
        ids = {it["id"] for it in its}
        if keep.get(k) in ids:
            homes[k] = keep[k]
        else:
            homes[k] = min(its, key=lambda it: (-len(it["meta"].get("linked_from") or []), it.get("chars", 0),
                                                it["id"]))["id"]
    return homes


def pointers(it: dict, homes: dict[str, str]) -> dict[str, str]:
    """{tab key: home id} for the tabs of `it` whose rows live in another workbook."""
    return {k: homes[k] for _, k in it.get("tabs") or [] if k and k in homes and homes[k] != it["id"]}


def point(text: str, ptrs: dict[str, str], titles: dict[str, str]) -> str:
    """A workbook's text with each tab whose rows live elsewhere (`ptrs`) as a pointer."""
    if not ptrs:
        return text
    out = []
    for part, (name, k) in zip(_TAB_SPLIT.split(text), tab_keys(text), strict=True):
        if k in ptrs:
            h = ptrs[k]
            out.append(f"## Sheet: {name}\n(Identical to the tab \"{name}\" in {h} \"{titles.get(h, '')}\"; "
                       f"its rows are there.)\n\n")
        else:
            out.append(part)
    return "".join(out)

