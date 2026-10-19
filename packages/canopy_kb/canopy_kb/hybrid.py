"""The semantic half of `KB.search`: what a passage's vector is made from, and how
its nearest neighbours are fused with BM25.

The KB file holds no vectors. A host that keeps them (canopy-web: pgvector, one row
per passage) embeds `embedding_text(...)` for each of `KB.passages()`, diffs by
`passage_sha(...)` so an unchanged passage is never embedded twice, and hands
`KB.search` a `semantic` callable that answers nearest passages for a query. With
no callable, or one that returns None, search is BM25 alone — nothing breaks
before the vectors exist.

Fusion is reciprocal rank fusion (RRF): each list contributes 1/(k + rank) per
item, so neither BM25's scores nor cosine distances need calibrating against each
other. k=60 is the value from the original paper and the usual default.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Iterable, Optional, Sequence

#: What one search may run: BM25 only, vectors only, or both fused.
MODES = ("hybrid", "lexical", "semantic")
RRF_K = 60
#: How deep each list goes before fusing. A good passage ranked 40th by one
#: method and 3rd by the other should still make the fused top 10.
CANDIDATES = 50
#: The embedding input is capped (the models truncate at a few thousand tokens);
#: a passage is at most ~1,800 chars, so the header and passage fit.
TEXT_MAX = 2000
TITLE_MAX = 200

#: `semantic(query, types=, since=, until=, limit=)` → [(item_id, passage_ord)]
#: nearest first, or None when there are no vectors to ask.
Semantic = Callable[..., Optional[Sequence[tuple[str, int]]]]


def embedding_text(title: str, type_: str, date: str, body: str) -> str:
    """The text embedded for one passage: a deterministic header, then the passage.

    The header carries what a passage deep inside a long document no longer says
    about itself (which report, what kind, when) — the same title on every
    passage, not only the first, unlike the FTS index."""
    head = [f"Title: {title.strip()[:TITLE_MAX]}" if (title or "").strip() else "",
            f"Type: {type_.replace('_', ' ')}" if type_ else "",
            f"Date: {date}" if date else ""]
    head_text = "\n".join(h for h in head if h)
    body = (body or "").strip()
    text = f"{head_text}\n\n{body}" if head_text and body else (head_text or body)
    return text[:TEXT_MAX]


def passage_sha(text: str) -> str:
    """What a stored vector is keyed by: a change to the passage or its header
    re-embeds it; nothing else does."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rrf(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> list[str]:
    """Reciprocal rank fusion of ranked id lists, best first. Ties keep the order
    in which an id was first met (the first list wins)."""
    score: dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            score[key] = score.get(key, 0.0) + 1.0 / (k + rank)
    order = {key: i for i, key in enumerate(score)}
    return sorted(score, key=lambda key: (-score[key], order[key]))
