"""Passage vectors: what one would be made from and keyed by. DORMANT — no host uses it.

Search is agentic and needs no vectors: words over passages and catalog cards, ordered
by a reranker, then the agent opens and greps documents (`research.py`). Measured on 70
GiveWell questions, that beat embedding retrieval outright; vectors added nothing it
could measure. This module, `KB.passages()` and the `semantic` parameter of
`research.search` are kept so a host can turn vectors back on without a schema change
if a corpus ever needs them (canopy-web: `KB_VECTORS`, off).

A host that keeps them embeds `embedding_text(...)` for each of `KB.passages()`, diffs
by `passage_sha(...)` so an unchanged passage is never embedded twice, and hands
`research.search` a `semantic` callable (`Semantic`) whose nearest passages join the
candidate pool before reranking. The KB file itself holds no vectors.
"""
from __future__ import annotations

import hashlib
from typing import Callable, Optional, Sequence

#: The embedding input is capped (gemini-embedding-001 takes 2,048 tokens); a
#: passage is at most 3,200 chars, so the header, context line and passage fit.
TEXT_MAX = 4000
TITLE_MAX = 200

#: `semantic(query, types=, since=, until=, limit=)` → [(item_id, passage_ord)]
#: nearest first, or None when there are no vectors to ask.
Semantic = Callable[..., Optional[Sequence[tuple[str, int]]]]


def embedding_text(title: str, type_: str, date: str, body: str, heading: str = "", context: str = "") -> str:
    """The text embedded for one passage: a deterministic header, then the passage.

    The header carries what a passage deep inside a long document no longer says
    about itself (which report, what kind, when, which section, and the curator's
    one-line context when there is one) — on every passage, not only the first,
    unlike the FTS index."""
    head = [f"Title: {title.strip()[:TITLE_MAX]}" if (title or "").strip() else "",
            f"Type: {type_.replace('_', ' ')}" if type_ else "",
            f"Date: {date}" if date else "",
            f"Section: {heading.strip()[:TITLE_MAX]}" if (heading or "").strip() else "",
            (context or "").strip()]
    head_text = "\n".join(h for h in head if h)
    body = (body or "").strip()
    text = f"{head_text}\n\n{body}" if head_text and body else (head_text or body)
    return text[:TEXT_MAX]


def passage_sha(text: str) -> str:
    """What a stored vector is keyed by: a change to the passage or its header
    re-embeds it; nothing else does."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
