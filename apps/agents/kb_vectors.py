"""DORMANT: knowledge-base vectors in pgvector. Nothing uses them unless KB_VECTORS is on,
and it is off.

KB search is agentic (`canopy_kb.research`): words over passages and catalog cards,
ordered by the reranker (`kb_rerank.py`), then the agent opens and greps documents.
On 70 GiveWell questions that beat one-shot embedding search outright, so vectors were
switched off rather than deleted: this module, `KbPassage` and `manage.py kb_embed`
stay so a corpus that turns out to need search by meaning can have it back with one
setting. While `KB_VECTORS` is off, `available()` is False, so `semantic_for` answers
None (the MCP `search` passes no vectors), a publish starts no sync, and `kb_embed`
refuses — no embedding call is made and no passage leaves the server for one.

What it does when on:

* **What is embedded** is `canopy_kb.vectors.embedding_text` for each passage of the
  file (a title/type/date header, then the passage), with
  `settings.KB_EMBEDDINGS_MODEL` (Vertex `gemini-embedding-001` at its full
  3072 dimensions, `RETRIEVAL_DOCUMENT`) through the same adapter, identity and zero-retention
  gate as memory (`apps/contacts/embeddings.py`). The KB is a publisher's public
  material; the gate holds anyway.
* **Sync** (`sync`) diffs the file against `KbPassage` by each passage's sha256:
  only new or changed passages are embedded, removed ones are deleted. Rows are
  written a batch at a time, so an interrupted run resumes where it stopped. A
  publish starts one in the background (`sync_after_publish`) that embeds at most
  `KB_SYNC_MAX_EMBED` passages — a KB's first publish is a ~140k-call job that
  waits for `manage.py kb_embed`, run on purpose.
* **Search** (`semantic_for`) embeds the question (`RETRIEVAL_QUERY`) and asks
  the HNSW index for the nearest passages of THIS project, filtered by type and
  date during the scan (pgvector 0.8 iterative scan). `canopy_kb` adds them to the
  pool the reranker orders.
Every way this can be missing — KB_VECTORS off, SQLite, no `vector` extension, a
non-Vertex model, no rows yet, the embedding call failing — answers None, and search
runs without vectors, as it does today.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from django.conf import settings
from django.db import DatabaseError, close_old_connections, connection, transaction

from apps.contacts import embeddings

from .models import KbPassage

logger = logging.getLogger(__name__)

#: gemini-embedding-001's full size. Smaller sizes are truncations of it (Matryoshka);
#: the full vector is its best, and halfvec indexes up to 4,000 dimensions.
DIMENSIONS = 3072
#: Rows written per round trip; also how much work an interrupted run loses.
BATCH = 256
#: Passages fetched per semantic search: one item can own several of the nearest,
#: and canopy_kb keeps one per item, so ask for more than the items wanted.
PASSAGES_PER_ITEM = 3
#: HNSW candidate list per search: pgvector's maximum. Measured on the GiveWell KB
#: (140k passages, the retired kb_eval): lower values lost neighbours an exact scan finds; with
#: the index built m=32, ef_construction=200 (migration 0065) 1000 comes within
#: 2/312 of the exact scan at ~60 ms a search.
EF_SEARCH = 1000
#: How hard a bulk embed tries one passage before leaving it for the next run.
ATTEMPTS = 6
#: Namespace of the advisory lock that keeps two syncs of one project apart.
_LOCK_NS = 0x6B62  # "kb"

_extension = {"checked": False, "ok": False}


def model() -> str:
    """The KB embedding model, or "" when it cannot fill the halfvec(3072) column."""
    m = (getattr(settings, "KB_EMBEDDINGS_MODEL", "") or "").strip()
    return m if m.startswith(embeddings.VERTEX_PREFIX) else ""


def available() -> bool:
    """KB_VECTORS on, PostgreSQL with the `vector` extension, and a usable model. The
    extension check is asked once per process: it does not come and go."""
    if not getattr(settings, "KB_VECTORS", False) or connection.vendor != "postgresql" or not model():
        return False
    if not _extension["checked"]:
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                _extension["ok"] = cur.fetchone() is not None
        except DatabaseError:
            _extension["ok"] = False
        _extension["checked"] = True
    return _extension["ok"]


# --- search -----------------------------------------------------------------------


def _unit(vector: bytes) -> list[float]:
    return list(embeddings.unpack(vector))


def search(project, query: str, *, types=None, since: str = "", until: str = "",
           limit: int = 50) -> list[tuple[str, int]] | None:
    """The project's nearest passages to `query`, nearest first, as (item_id, ord);
    None when there is nothing to ask (see the module docstring)."""
    if not available():
        return None
    model_id = model()
    rows = KbPassage.objects.filter(project=project, embedding_model=model_id)
    if not rows.exists():
        return None
    got = embeddings.embed([query], purpose="query", model_id=model_id, dimensions=DIMENSIONS)
    if not got:
        return None
    from pgvector import HalfVector
    from pgvector.django import CosineDistance

    if types:
        rows = rows.filter(item_type__in=list(types))
    if since:
        rows = rows.filter(date__gte=since)
    if until:
        rows = rows.filter(date__lte=until)
    want = max(1, limit) * PASSAGES_PER_ITEM
    try:
        with transaction.atomic(), connection.cursor() as cur:
            # Filtered HNSW: keep scanning until `want` rows pass the filters,
            # instead of returning whatever survives of the first ef_search.
            cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            cur.execute("SET LOCAL hnsw.ef_search = %s", [max(EF_SEARCH, want)])
            hits = list(rows.annotate(distance=CosineDistance("embedding", HalfVector(_unit(got[0]))))
                        .order_by("distance").values_list("item_id", "ord", "distance")[:want])
    except DatabaseError:
        logger.warning("kb vectors: search failed for project %s; lexical instead", project.pk, exc_info=True)
        return None
    hits.sort(key=lambda h: h[2])  # relaxed_order may hand them back slightly out of order
    return [(item_id, ord_) for item_id, ord_, _ in hits]


def semantic_for(project) -> Callable | None:
    """The `semantic` callable `canopy_kb` search takes, bound to this project."""
    if not available():
        return None

    def semantic(query, *, types=None, since="", until="", limit=50):
        return search(project, query, types=types, since=since, until=until, limit=limit)

    return semantic


# --- sync -------------------------------------------------------------------------


class _Pace:
    """At most `per_minute` calls a minute across threads, and a shared back-off
    when the provider says 429."""

    def __init__(self, per_minute: int):
        self.gap = 60.0 / max(1, per_minute)
        self.next_at = time.monotonic()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.gap
        if at > now:
            time.sleep(at - now)

    def back_off(self, seconds: float):
        with self.lock:
            self.next_at = max(self.next_at, time.monotonic() + seconds)


class WrongDimensions(RuntimeError):
    """The model answers vectors the column cannot hold: a configuration error, not a retry."""


def _embed_one(text: str, model_id: str, pace: _Pace) -> list[float] | None:
    delay = 1.0
    for _ in range(ATTEMPTS):
        pace.wait()
        try:
            vector = embeddings.invoke(model_id, [text], "document", dimensions=DIMENSIONS)[0]
        except Exception as exc:  # noqa: BLE001 — a timeout, a 429, a 5xx: wait and retry
            if getattr(getattr(exc, "response", None), "status_code", None) == 429:
                pace.back_off(delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        if len(vector) != DIMENSIONS:
            raise WrongDimensions(f"{model_id} returned {len(vector)} dimensions, not {DIMENSIONS}")
        return vector
    return None


def _try_lock(project) -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s, %s)", [_LOCK_NS, project.pk])
        return bool(cur.fetchone()[0])


def _unlock(project):
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", [_LOCK_NS, project.pk])


def sync(project, *, max_embed: int | None = None, workers: int | None = None,
         per_minute: int | None = None, dry_run: bool = False,
         progress: Callable[[dict], None] | None = None) -> dict:
    """Bring the project's vectors in step with its published KB file.

    Returns counts: `passages` in the file, `kept`, `embedded`, `failed`, `removed`,
    and `deferred` (passages left unembedded because there were more than
    `max_embed`). Raises RuntimeError when the provider is not verified to keep
    nothing, or when a whole batch fails (the service is down; stop, don't burn
    through the file)."""
    from . import kb_store

    model_id = model()
    if not model_id:
        raise RuntimeError("KB_EMBEDDINGS_MODEL is not a vertex/ model; KB vectors are off")
    if not dry_run:
        ok, why = embeddings.retention_ok(model_id=model_id)
        if not ok:
            raise RuntimeError(f"not sending passages to {model_id}: {why}")
    if not _try_lock(project):
        return {"skipped": "a sync of this project is already running"}
    try:
        return _sync(project, kb_store.open_kb(project), model_id, max_embed=max_embed,
                     workers=workers or settings.KB_EMBED_WORKERS,
                     per_minute=per_minute or settings.KB_EMBED_PER_MINUTE,
                     dry_run=dry_run, progress=progress)
    finally:
        _unlock(project)


def _sync(project, kb, model_id, *, max_embed, workers, per_minute, dry_run, progress) -> dict:
    try:
        # Pass one: what the file has, keyed as the table keys it. No text kept.
        want: dict[tuple[str, int], dict] = {}
        for p in kb.passages():
            want[(p["item_id"], p["ord"])] = {k: p[k] for k in ("sha256", "type", "date", "title", "url")}
        have = {(i, o): (pk, sha, m, url) for pk, i, o, sha, m, url in KbPassage.objects.filter(
            project=project).values_list("pk", "item_id", "ord", "sha256", "embedding_model", "url")}
        gone = [v[0] for k, v in have.items() if k not in want]
        todo = {k for k, w in want.items()
                if k not in have or have[k][1] != w["sha256"] or have[k][2] != model_id}
        moved = [(have[k][0], w["url"]) for k, w in want.items()
                 if k in have and k not in todo and have[k][3] != w["url"]]
        out = {"passages": len(want), "kept": len(want) - len(todo), "embedded": 0, "failed": 0,
               "removed": len(gone), "deferred": 0, "model": model_id}
        if dry_run:
            out["to_embed"] = len(todo)
            return out
        for i in range(0, len(gone), 5000):
            KbPassage.objects.filter(pk__in=gone[i:i + 5000]).delete()
        for pk, url in moved:  # the url is not in the embedded text: no new vector needed
            KbPassage.objects.filter(pk=pk).update(url=url)
        if max_embed is not None and len(todo) > max_embed:
            out["deferred"] = len(todo)
            logger.info("kb vectors: project %s has %d passages to embed (> %d); run "
                        "`manage.py kb_embed` for it", project.pk, len(todo), max_embed)
            return out
        if not todo:
            return out

        # Pass two: stream the file again, embedding the passages that need it.
        pace = _Pace(per_minute)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            batch: list[dict] = []
            for p in kb.passages():
                if (p["item_id"], p["ord"]) not in todo:
                    continue
                batch.append(p)
                if len(batch) >= BATCH:
                    _write(project, batch, pool, model_id, pace, out)
                    batch = []
                    if progress:
                        progress(out)
            if batch:
                _write(project, batch, pool, model_id, pace, out)
                if progress:
                    progress(out)
        return out
    finally:
        kb.db.close()


def _write(project, batch, pool, model_id, pace, out):
    vectors = list(pool.map(lambda p: _embed_one(p["text"], model_id, pace), batch))
    rows = [KbPassage(project=project, item_id=p["item_id"], ord=p["ord"], sha256=p["sha256"],
                      item_type=p["type"], date=p["date"], title=p["title"][:2000], url=p["url"][:1000],
                      embedding=v, embedding_model=model_id)
            for p, v in zip(batch, vectors) if v is not None]
    failed = len(batch) - len(rows)
    if rows:
        KbPassage.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["project", "item_id", "ord"],
            update_fields=["sha256", "item_type", "date", "title", "url", "embedding", "embedding_model",
                           "updated_at"])
    out["embedded"] += len(rows)
    out["failed"] += failed
    if not rows:
        raise RuntimeError(f"every passage in a batch of {len(batch)} failed to embed; stopping "
                           f"after {out['embedded']} (rerun to resume)")


# --- after a publish --------------------------------------------------------------


def sync_after_publish(project) -> bool:
    """Start a background sync once the publish commits. False when KB vectors
    are off here (nothing to keep in step)."""
    if not available():
        return False
    pk = project.pk
    transaction.on_commit(lambda: _start(pk))
    return True


def _start(project_pk: int):
    threading.Thread(target=_background_sync, args=(project_pk,), daemon=True,
                     name=f"kb-sync-{project_pk}").start()


def _background_sync(project_pk: int):
    from .models import AgentProject

    close_old_connections()
    try:
        project = AgentProject.objects.get(pk=project_pk)
        result = sync(project, max_embed=settings.KB_SYNC_MAX_EMBED)
        logger.info("kb vectors: synced project %s after publish: %s", project_pk, result)
    except Exception:  # noqa: BLE001 — a failed sync leaves search lexical; the next run resumes
        logger.warning("kb vectors: sync after publish failed for project %s", project_pk, exc_info=True)
    finally:
        close_old_connections()
