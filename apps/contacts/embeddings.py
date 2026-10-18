"""Vectors for finding memory by meaning (docs/architecture/hcp-service.md § Retrieval).

Off unless `settings.HCP_EMBEDDINGS_MODEL` names a model. When on, every current
entry carries a vector of its text, made by that model on AWS Bedrock (canopy's own
AWS account; nothing leaves it), and `hcp.search` ranks by meaning as well as by
shared words. The sentence stays the source of truth: a vector is only for finding,
lives on the entry's own row (a hard delete takes it too), and is cleared the moment
its version stops being served.

Vectors from different models never mix: a row whose `embedding_model` is not the
current one counts as having none, and is re-embedded the next time a search meets
it (or by `manage.py hcp_embed`). Every failure here falls back to lexical search;
memory never breaks because the embedding API is down.
"""
from __future__ import annotations

import json
import logging
import math
from array import array

from django.conf import settings

logger = logging.getLogger(__name__)

#: Cohere takes at most 96 texts per call; Titan takes one.
COHERE_BATCH = 96
#: How many missing vectors one search will fill before ranking. Beyond that the
#: rest rank lexically this time and are filled by later searches (or hcp_embed).
FILL_PER_SEARCH = 96
#: Text sent per entry is capped (Cohere truncates at ~512 tokens anyway).
TEXT_MAX = 2000

_client = None


def model() -> str:
    """The model in use, or "" when embeddings are off."""
    return (getattr(settings, "HCP_EMBEDDINGS_MODEL", "") or "").strip()


def enabled() -> bool:
    return bool(model())


def _bedrock():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        # Short timeouts: a search waits on this, and lexical is always there to fall back to.
        _client = boto3.client(
            "bedrock-runtime", region_name=getattr(settings, "HCP_EMBEDDINGS_REGION", "") or None,
            config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}))
    return _client


def _invoke(model_id: str, texts: list[str], purpose: str) -> list[list[float]]:
    """One Bedrock call per batch. `purpose` is "document" (an entry) or "query" (a
    search) — Cohere embeds them differently; Titan has one space for both."""
    out: list[list[float]] = []
    if model_id.startswith("cohere."):
        input_type = "search_query" if purpose == "query" else "search_document"
        for i in range(0, len(texts), COHERE_BATCH):
            body = {"texts": texts[i:i + COHERE_BATCH], "input_type": input_type, "truncate": "END",
                    "embedding_types": ["float"]}
            resp = _bedrock().invoke_model(modelId=model_id, body=json.dumps(body))
            got = json.loads(resp["body"].read())["embeddings"]
            # Asked for by type (v3 and v4 both accept it), the vectors come keyed by type.
            out.extend(got["float"] if isinstance(got, dict) else got)
        return out
    if model_id.startswith("amazon.titan-embed"):
        for text in texts:
            body = {"inputText": text, "normalize": True}
            resp = _bedrock().invoke_model(modelId=model_id, body=json.dumps(body))
            out.append(json.loads(resp["body"].read())["embedding"])
        return out
    raise ValueError(f"no Bedrock embedding adapter for {model_id!r} (cohere.* or amazon.titan-embed*)")


def embed(texts: list[str], *, purpose: str = "document") -> list[bytes] | None:
    """Packed, unit-length vectors for `texts`, or None when off or on any failure."""
    model_id = model()
    if not model_id or not texts:
        return None
    try:
        vectors = _invoke(model_id, [t[:TEXT_MAX] for t in texts], purpose)
    except Exception:  # noqa: BLE001 — any failure means "rank lexically this time"
        logger.warning("hcp embeddings: %s failed; falling back to lexical", model_id, exc_info=True)
        return None
    if len(vectors) != len(texts):
        logger.warning("hcp embeddings: %s returned %d vectors for %d texts", model_id,
                       len(vectors), len(texts))
        return None
    return [pack(v) for v in vectors]


def pack(vector: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array("f", (x / norm for x in vector)).tobytes()


def unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(bytes(blob))
    return a


def similarity(a: bytes, b: bytes) -> float:
    """Cosine similarity of two packed vectors (both unit length, so the dot product)."""
    x, y = unpack(a), unpack(b)
    if len(x) != len(y):
        return 0.0
    return sum(p * q for p, q in zip(x, y))


def text_of(fact) -> str:
    """What is embedded for an entry: the sentence, then what it is about."""
    category = fact.category.removeprefix("hcp-custom:").replace("_", " ")
    parts = [fact.statement, fact.dimension.replace("_", " ") if fact.dimension else "",
             fact.instance_ref, fact.project.name if fact.project_id else "", category]
    return ". ".join(p for p in parts if p)


def has_current(fact) -> bool:
    return bool(fact.embedding) and fact.embedding_model == model()


def fill(facts: list, *, limit: int = FILL_PER_SEARCH) -> int:
    """Embed the entries among `facts` that have no vector from the current model,
    store them, and return how many were filled."""
    from .models import PersonFact

    missing = [f for f in facts if not has_current(f)][:limit]
    if not missing:
        return 0
    vectors = embed([text_of(f) for f in missing])
    if vectors is None:
        return 0
    model_id = model()
    for f, v in zip(missing, vectors):
        f.embedding, f.embedding_model = v, model_id
        # Only a row that is still current gets one: a version superseded or deleted
        # meanwhile keeps none (vectors are personal data; nothing finds by a dead one).
        PersonFact.objects.filter(pk=f.pk, superseded_at__isnull=True, retracted_at__isnull=True
                                  ).update(embedding=v, embedding_model=model_id)
    return len(missing)
