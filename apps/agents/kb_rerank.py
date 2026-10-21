"""The reranker a workspace project's knowledge-base `search` orders its candidates with.

`canopy_kb.research.search` pools passages that match the question's words and documents
whose catalog card matches (up to ~120), and hands them here: the Vertex AI ranking API
(off unless `KB_RERANK_MODEL` is set) reads the question and each candidate together and
answers the order. It is a ranking model, not embeddings — nothing is stored, and no
vectors are involved (those are dormant, `kb_vectors.py`).

There is no retention setting to check the way `retention_ok` checks an embedding model:
Google publishes no zero-retention statement for ranking and no cacheConfig-like switch.
What it does say: the API is "stateless" (ranking page); Agent Search, which it belongs
to, is an AI/ML Service, not a Generative AI Service, so the abuse-monitoring prompt
logging does not cover it (Service Specific Terms, services summary); "Google will not
use Customer Data to train or fine-tune any AI/ML models" (Service Specific Terms §18).
Its Data Access audit logs (`RankService.Rank`, DATA_READ) are off unless the project
turns them on, and land in our own project. Needs `discoveryengine.googleapis.com`
enabled and `discoveryengine.rankingConfigs.rank` on the service account.

Calls go to the US multi-region (`us-discoveryengine.googleapis.com`, `locations/us`;
`KB_RERANK_LOCATION`): "Agent Search on Gemini Enterprise Agent Platform" is on Google's
HIPAA BAA list with the footnote "Use regional APIs and resource locations for PHI",
which `global` is not. Same scores and latency as `global` (checked 2026-10-19, 50
passages: ~0.45 s both). `semantic-ranker-default@latest` beat `-fast` on paraphrased
questions (GiveWell KB, 312 questions: MRR 0.588 against 0.545). About 0.5 s a call.
"""
from __future__ import annotations

import logging
from typing import Callable

from django.conf import settings

from apps.contacts import embeddings

logger = logging.getLogger(__name__)


def _rank_location() -> str:
    return (getattr(settings, "KB_RERANK_LOCATION", "") or "us").strip()


def _rank_host() -> str:
    """`global` is the plain host; a multi-region (`us`, `eu`) has its own, which is
    what Google's BAA asks for with PHI ("use regional APIs and resource locations")."""
    loc = _rank_location()
    return "discoveryengine.googleapis.com" if loc == "global" else f"{loc}-discoveryengine.googleapis.com"


def _rank(query: str, docs: list[dict]) -> list[int] | None:
    """Vertex AI ranking API (Discovery Engine `rank`) over the candidates: a model
    that reads the question and each passage together, which a word index cannot do.
    Indices into `docs`, best first; None on any failure (the word order stands)."""
    url = (f"https://{_rank_host()}/v1/projects/"
           f"{settings.HCP_VERTEX_PROJECT}/locations/{_rank_location()}/rankingConfigs/default_ranking_config:rank")
    records = [{"id": str(i), "title": d["title"][:500], "content": d["text"]} for i, d in enumerate(docs)]
    try:
        resp = embeddings._vertex_session().post(
            url, json={"model": settings.KB_RERANK_MODEL, "query": query[:2000], "records": records,
                       "topN": len(records)}, timeout=(2, 15))  # a research search sends up to ~120
        resp.raise_for_status()
        return [int(r["id"]) for r in resp.json().get("records", [])]
    except Exception:  # noqa: BLE001 — reranking improves a usable order; it never costs one
        logger.warning("kb rerank: %s failed; keeping the word order", settings.KB_RERANK_MODEL, exc_info=True)
        return None


def reranker() -> Callable | None:
    """The `rerank` callable `canopy_kb` search takes, when KB_RERANK_MODEL names one."""
    return _rank if (getattr(settings, "KB_RERANK_MODEL", "") or "").strip() else None
