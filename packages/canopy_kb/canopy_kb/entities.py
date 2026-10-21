"""The entity index: organisations, programs and countries, each with every document
that is substantially about it.

Names come from the items' catalog cards (`card.orgs`, `card.programs`,
`card.countries`, written by the curator). They are merged three ways: by a
normalised key ("The Against Malaria Foundation (AMF)" = "against malaria
foundation"), by acronym (a card naming "AMF" joins the group that spelled it out),
and by the curator's alias groups (`aliases.json`), which catch what no string rule
can: renames (SCI Foundation → Unlimit Health) and synonyms (malaria nets =
insecticide-treated nets).
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

KINDS = (("org", "orgs"), ("program", "programs"), ("country", "countries"))
_MAX_NAME = 120


def key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(inc|ltd|llc|org)\b", " ", s)
    return " ".join(s.split())


def _acronym(name: str) -> str:
    m = re.search(r"\(([A-Z][A-Za-z0-9&-]{1,10})\)", name)
    return m.group(1) if m else ""


def index(cards: dict[str, dict], aliases: dict | None = None,
          url_orgs: dict[str, str] | None = None) -> tuple[list[dict], dict[str, set[int]]]:
    """Entities ([{kind, name, aliases, items}]) and, per item, the entities it is about.

    `url_orgs` names an item's organisation where its URL does (a charity's pages)."""
    groups: dict[tuple[str, str], dict] = {}
    acr_owner: dict[tuple[str, str], Counter] = defaultdict(Counter)
    mentions: list[tuple[str, str, str]] = []

    def add(iid: str, kind: str, name: str) -> None:
        name = name.strip()
        k = key(name)
        if not k or len(name) > _MAX_NAME:
            return
        g = groups.setdefault((kind, k), {"names": Counter(), "acr": Counter()})
        g["names"][name] += 1
        a = _acronym(name)
        if a:
            g["acr"][a] += 1
            acr_owner[(kind, a.lower())][k] += 1
        mentions.append((iid, kind, k))

    for iid, card in cards.items():
        for kind, field in KINDS:
            for name in card.get(field) or []:
                if isinstance(name, str):
                    add(iid, kind, name)
    for iid, name in (url_orgs or {}).items():
        add(iid, "org", name)

    remap: dict[tuple[str, str], tuple[str, str]] = {}
    for (kind, k) in groups:  # a name that IS a known acronym joins the group that spelled it out
        owner = acr_owner.get((kind, k))
        if owner and k not in owner:
            remap[(kind, k)] = (kind, owner.most_common(1)[0][0])
    for kind, gs in (aliases or {}).items():
        for g in gs or []:
            keys = sorted({key(n) for n in g.get("names") or [] if (kind, key(n)) in groups})
            if len(keys) < 2:
                continue
            ck = key(g.get("canonical") or "")
            tgt = ck if ck in keys else max(keys, key=lambda x: sum(groups[(kind, x)]["names"].values()))
            for k in keys:
                if k != tgt:
                    remap[(kind, k)] = (kind, tgt)
            if g.get("canonical"):
                groups[(kind, tgt)]["names"][g["canonical"]] += 10 ** 6  # shown as the entity's name
    for gk in list(remap):  # follow chains so every member lands on a group that is not remapped
        seen, t = {gk}, remap[gk]
        while t in remap and t not in seen:
            seen.add(t)
            t = remap[t]
        remap[gk] = t

    ents: list[dict] = []
    idx: dict[tuple[str, str], int] = {}
    for gk, g in groups.items():
        if gk in remap:
            continue
        idx[gk] = len(ents)
        name = g["names"].most_common(1)[0][0]
        ents.append({"kind": gk[0], "name": re.sub(r"\s*\([A-Z][A-Za-z0-9&-]{1,10}\)\s*$", "", name) or name,
                     "aliases": set(g["names"]) | set(g["acr"]), "items": set()})
    for gk, tgt in remap.items():
        if tgt in idx:
            idx[gk] = idx[tgt]
            ents[idx[tgt]]["aliases"] |= set(groups[gk]["names"]) | set(groups[gk]["acr"])
    by_item: dict[str, set[int]] = defaultdict(set)
    for iid, kind, k in mentions:
        e = idx.get((kind, k))
        if e is not None:
            by_item[iid].add(e)
            ents[e]["items"].add(iid)
    for e in ents:
        e["aliases"] = sorted(e["aliases"])
    return ents, by_item
