"""Vectors for finding memory by meaning (docs/architecture/hcp-service.md § Retrieval).

Off unless `settings.HCP_EMBEDDINGS_MODEL` names a model:

* `vertex/<model>` — Google Vertex AI (e.g. `vertex/gemini-embedding-001`), reached
  from the ECS task with workload identity federation: the task's own AWS role is
  exchanged for a short-lived Google token, so no Google key is stored anywhere.
* a bare Bedrock model id (`cohere.embed-v4:0`, `amazon.titan-embed-text-v2:0`) — AWS
  Bedrock in canopy's own account.

When on, every current entry carries a vector of its text and `hcp.search` ranks by
meaning as well as by shared words. The sentence stays the source of truth: a
vector is only for finding, lives on the entry's own row (a hard delete takes it
too), and is cleared the moment its version stops being served.

**Nothing is sent until the provider is verified to keep nothing** (`retention_ok`):
on Vertex, the project's cache config must say `disableCache: true`; on Bedrock,
the account must have no model-invocation logging. Checked before the first call
and again every `RETENTION_TTL` seconds; a failed or unverifiable check means
lexical search, not a guess.

Vectors from different models never mix: a row whose `embedding_model` is not the
current one counts as having none, and is re-embedded the next time a search meets
it (or by `manage.py hcp_embed`). Every failure here falls back to lexical search;
memory never breaks because the embedding API is down.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from array import array

from django.conf import settings

logger = logging.getLogger(__name__)

#: Cohere takes at most 96 texts per call; Titan and gemini-embedding take one.
COHERE_BATCH = 96
#: Vertex's text-embedding-* models take up to 250 instances per call.
VERTEX_TEXT_BATCH = 250
#: Vector size asked of models that let us choose (Gemini: 768, 1536 or 3072).
VERTEX_DIMENSIONS = 768
#: How many missing vectors one search will fill before ranking. Beyond that the
#: rest rank lexically this time and are filled by later searches (or hcp_embed).
FILL_PER_SEARCH = 96
#: Text sent per entry is capped (the models truncate at a few thousand tokens anyway).
TEXT_MAX = 2000
#: How long a passed retention check is trusted before it is asked again.
RETENTION_TTL = 900
VERTEX_PREFIX = "vertex/"

_clients: dict = {}
_retention: dict = {"model": None, "ok": False, "why": "not checked", "at": 0.0}
_lock = threading.Lock()


def model() -> str:
    """The model in use, or "" when embeddings are off."""
    return (getattr(settings, "HCP_EMBEDDINGS_MODEL", "") or "").strip()


def enabled() -> bool:
    return bool(model())


def provider(model_id: str | None = None) -> str:
    model_id = model() if model_id is None else model_id
    return "vertex" if model_id.startswith(VERTEX_PREFIX) else "bedrock"


# --- providers --------------------------------------------------------------------


def _bedrock(service: str = "bedrock-runtime"):
    if service not in _clients:
        import boto3
        from botocore.config import Config

        # Short timeouts: a search waits on this, and lexical is always there to fall back to.
        _clients[service] = boto3.client(
            service, region_name=getattr(settings, "HCP_EMBEDDINGS_REGION", "") or None,
            config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}))
    return _clients[service]


def _invoke_bedrock(model_id: str, texts: list[str], purpose: str) -> list[list[float]]:
    """`purpose` is "document" (an entry) or "query" (a search) — Cohere embeds them
    differently; Titan has one space for both."""
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


class _TaskRoleCredentials:
    """google-auth's AWS supplier, fed from boto3: on ECS the task role's credentials
    come from the container endpoint, which google-auth cannot read on its own."""

    def get_aws_security_credentials(self, context, request):
        import boto3
        from google.auth import aws

        c = boto3.Session().get_credentials().get_frozen_credentials()
        return aws.AwsSecurityCredentials(c.access_key, c.secret_key, c.token)

    def get_aws_region(self, context, request):
        import boto3

        return boto3.Session().region_name or getattr(settings, "AWS_REGION", "") or "us-east-1"


def _vertex_session():
    """An authorized HTTP session to Vertex as the configured service account, by
    workload identity federation from the task's AWS role. No stored Google key."""
    if "vertex" not in _clients:
        from google.auth import aws
        from google.auth.transport.requests import AuthorizedSession

        sa = settings.HCP_VERTEX_SERVICE_ACCOUNT
        creds = aws.Credentials(
            audience=settings.HCP_VERTEX_WIF_AUDIENCE,
            subject_token_type="urn:ietf:params:aws:token-type:aws4_request",
            aws_security_credentials_supplier=_TaskRoleCredentials(),
            service_account_impersonation_url=(
                "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
                f"{sa}:generateAccessToken"),
            scopes=["https://www.googleapis.com/auth/cloud-platform"])
        _clients["vertex"] = AuthorizedSession(creds)
    return _clients["vertex"]


def _vertex_base() -> str:
    loc = settings.HCP_VERTEX_LOCATION
    return f"https://{loc}-aiplatform.googleapis.com/v1/projects/{settings.HCP_VERTEX_PROJECT}"


def _invoke_vertex(model_id: str, texts: list[str], purpose: str) -> list[list[float]]:
    name = model_id.removeprefix(VERTEX_PREFIX)
    url = f"{_vertex_base()}/locations/{settings.HCP_VERTEX_LOCATION}/publishers/google/models/{name}:predict"
    task = "RETRIEVAL_QUERY" if purpose == "query" else "RETRIEVAL_DOCUMENT"
    # gemini-embedding takes one text per call; text-embedding-* take a batch.
    batch = VERTEX_TEXT_BATCH if name.startswith("text-") else 1
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        body = {"instances": [{"content": t, "task_type": task} for t in texts[i:i + batch]],
                "parameters": {"autoTruncate": True, "outputDimensionality": VERTEX_DIMENSIONS}}
        resp = _vertex_session().post(url, json=body, timeout=(2, 5))
        resp.raise_for_status()
        out.extend(p["embeddings"]["values"] for p in resp.json()["predictions"])
    return out


def _invoke(model_id: str, texts: list[str], purpose: str) -> list[list[float]]:
    if provider(model_id) == "vertex":
        return _invoke_vertex(model_id, texts, purpose)
    return _invoke_bedrock(model_id, texts, purpose)


# --- zero retention ---------------------------------------------------------------


def _check_retention(model_id: str) -> tuple[bool, str]:
    """(ok, why). Ok only when the provider says, right now, that it keeps nothing
    we send. Raises are the caller's to treat as "not verified"."""
    if provider(model_id) == "vertex":
        resp = _vertex_session().get(f"{_vertex_base()}/cacheConfig", timeout=(2, 5))
        resp.raise_for_status()
        if resp.json().get("disableCache") is True:
            return True, "Vertex caching is disabled for the project"
        return False, f"Vertex caching is on for project {settings.HCP_VERTEX_PROJECT} (cacheConfig)"
    cfg = _bedrock("bedrock").get_model_invocation_logging_configuration().get("loggingConfig")
    if not cfg:
        return True, "Bedrock model-invocation logging is off for the account"
    return False, "Bedrock model-invocation logging is on for the account; it would keep memory text"


def retention_ok(*, force: bool = False) -> tuple[bool, str]:
    """Is the current provider verified to keep nothing? Cached for RETENTION_TTL."""
    model_id = model()
    if not model_id:
        return False, "embeddings are off"
    with _lock:
        fresh = (_retention["model"] == model_id and time.monotonic() - _retention["at"] < RETENTION_TTL)
        if fresh and not force:
            return _retention["ok"], _retention["why"]
        try:
            ok, why = _check_retention(model_id)
        except Exception as exc:  # noqa: BLE001 — unverifiable is the same as not verified
            ok, why = False, f"could not verify retention: {exc.__class__.__name__}: {exc}"
        if not ok:
            logger.warning("hcp embeddings: not sending memory text to %s — %s", model_id, why)
        _retention.update(model=model_id, ok=ok, why=why, at=time.monotonic())
        return ok, why


def embed(texts: list[str], *, purpose: str = "document") -> list[bytes] | None:
    """Packed, unit-length vectors for `texts`, or None when off, unverified, or on
    any failure."""
    model_id = model()
    if not model_id or not texts:
        return None
    if not retention_ok()[0]:
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
