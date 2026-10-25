"""A catalog card for every item: what the document is, what it says, who and what it is
about. Cards are what a researching agent browses before it opens anything (search
results, entity pages and `doc` show them), and their names build the entity index.

The AI session running the refresh (fast subagents, for a batch) reads the title, the
documents that link to it, the outline and the first 7,000 and last 1,500 characters
(`prompt`) and returns the card, held to its shape by `check`. A curator can card
structured items without a model (`structured`), and a `dataset` item inherits its
parent's card. `attach` puts the cards on the items.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from canopy_kb.prep.raw import read_json

HEAD, TAIL = 7000, 1500
MAX_TOKENS = 2048
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
KINDS = ("charity review, grant writeup, intervention report, cost-effectiveness model, model inputs/data, "
         "conversation notes, site visit, board/meeting record, financial/administrative, blog post, podcast "
         "transcript, methodology/process, research paper/external report, donor-facing page, other")

PROMPT = """You are cataloguing one document from {publisher}'s public website and files, so a research
agent can decide whether to open it. Be specific and factual; use only what the excerpt shows.

Title: {title}
Type: {type}
URL: {url}
Date: {date}
Linked from: {linked}
Section headings: {headings}
Length: {n} characters

<excerpt>
{body}
</excerpt>

Return JSON:
{{
 "summary": "2-4 sentences: what this document is, its main content or conclusion, and any headline figures (cost-effectiveness, amounts, effect sizes) it states",
 "kind": "one of: {kinds}",
 "orgs": ["organizations the document is substantially about (charities, implementers, funders, researchers' institutions); full official names; at most 6"],
 "programs": ["interventions or programs it covers, generic names e.g. 'seasonal malaria chemoprevention', 'deworming', 'vitamin A supplementation', 'cash transfers'; at most 6"],
 "countries": ["countries or regions it covers; at most 8"],
 "period": "years the content refers to, e.g. '2019-2021', or ''",
 "superseded": "if the text says a newer version or review exists, say so briefly, else ''",
 "answers": ["3-5 specific questions this document answers well"]
}}"""

#: A card for an item without a model (e.g. a row of a grants list), or None.
Structured = Callable[[dict], Optional[dict]]
#: Adjust a card as it is attached (e.g. add the organisation a URL names).
Enrich = Callable[[dict, dict], dict]


def excerpt(text: str) -> str:
    if len(text) <= HEAD + TAIL + 200:
        return text
    return text[:HEAD] + f"\n\n[... {len(text) - HEAD - TAIL} characters omitted ...]\n\n" + text[-TAIL:]


def headings(text: str) -> str:
    seen, out = set(), []
    for h in (h.strip()[:80] for h in HEADING.findall(text)):
        if h not in seen:
            seen.add(h)
            out.append(h)
    if not out:
        return "(none)"
    return " | ".join(out[:60]) + (f" | ... ({len(out) - 60} more)" if len(out) > 60 else "")


def prompt(it: dict, linked: list[str], publisher: str, kinds: str = KINDS) -> str:
    """The card prompt for an item (its text in place); `linked`: the titles of what links to it."""
    return PROMPT.format(publisher=publisher, title=it["title"], type=it["type"], url=it["url"],
                         date=it.get("date", ""), linked="; ".join(linked) or "(none)", headings=headings(it["text"]),
                         n=len(it["text"]), body=excerpt(it["text"]), kinds=kinds)


def linked_titles(it: dict, titles: dict[str, str], by_url: dict[str, str]) -> list[str]:
    """The first five referrers of an item, by title where known (what the prompt shows)."""
    return [titles.get(x) or by_url.get(x) or x for x in (it["meta"].get("linked_from") or [])][:5]


#: A card's fields: free text, and lists of names.
TEXTS = ("summary", "kind", "period", "superseded")
LISTS = ("orgs", "programs", "countries", "answers")


def check(card) -> dict:
    """A card as a writer (a model, a session) returned it, held to PROMPT's shape:
    ValueError if it has no summary or a field of the wrong type. Absent fields are empty."""
    if isinstance(card, list) and len(card) == 1:
        card = card[0]
    if not isinstance(card, dict):
        raise ValueError("a card is a JSON object")
    if not isinstance(card.get("summary"), str) or not card["summary"].strip():
        raise ValueError("a card needs a summary")
    out = {}
    for f in TEXTS:
        v = card.get(f) if card.get(f) is not None else ""
        if not isinstance(v, str):
            raise ValueError(f"{f} must be a string")
        out[f] = v
    for f in LISTS:
        v = card.get(f) if card.get(f) is not None else []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ValueError(f"{f} must be a list of strings")
        out[f] = v
    return out


def dataset_card(parent: dict) -> dict:
    """A dataset item's card: its workbook's, said of its data tabs."""
    card = dict(parent)
    card["summary"] = "Data tabs of: " + card.get("summary", "")
    card["kind"] = "model inputs/data"
    return card


def attach(items: list[dict], data: Path, enrich: Enrich | None = None) -> int:
    """Put each item's card on it (a dataset takes its parent's). Returns how many have one."""
    cache = read_json(data / "cards.json", {})
    n = 0
    for it in items:
        src = it["meta"].get("parent") if it["type"] == "dataset" else it["id"]
        c = (cache.get(src) or {}).get("card")
        if not c:
            continue
        card = dataset_card(c) if it["type"] == "dataset" else dict(c)
        it["card"] = enrich(it, card) if enrich else card
        n += 1
    return n
