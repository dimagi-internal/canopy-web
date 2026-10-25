"""A workspace project's published knowledge base — where the bytes live and the
copy each serving instance reads.

The steward builds `kb.sqlite` (on its runner, from the served file and what changed)
and publishes the whole file here; canopy-web never builds one (#1971). It answers the
project's MCP tools from it (`apps/mcp/projects.py`). Readers never download anything:
they call the tools.

* **Upload** goes straight to S3 on a presigned PUT (`upload_target`), never
  through the web dyno — a KB is a few hundred MB compressed.
* **Publish** (`publish`) checks the object is there, under THIS project's
  prefix, then points the project at it (`point`). The previous object stays, as
  an earlier version's file (`kb_versions`, which prunes beyond the newest few).
* **Read** (`open_kb`): each instance keeps the decompressed copy on local disk,
  keyed by the published object's sha256, so a new publish is picked up on the
  next call and an unchanged one is never fetched twice.
* **Originals** (`originals_upload`, `original_link`): the files each document's text
  was read from (PDFs, spreadsheets, pages), stored under the project's
  `originals/<sha256>`. The steward uploads only what the store lacks, on presigned
  PUTs that S3 checks against the sha256; the `original` tool hands a reader a
  15-minute download link. Never deleted: a file the publisher changed or removed is
  the version a KB's `originals` table still lists, and the reason to keep originals.
* **Curation** (`results_upload`, `stored`, `links`): what a refresh needs from the
  last one, so it can run on any machine and fetch only what it changes
  (`canopy_kb.curate`): step results (an extracted text, a card) gzipped under
  `results/<step key>`, immutable, and the curation states made of them, each named by
  the version it built (`KbVersion.curation_sha`). Steward only.

The bucket is `CANOPY_KB_BUCKET`, else the attachments bucket (same access: a
bucket policy naming the task role). No bucket → `KbNotConfigured`, a clear 503,
never a half-published project.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

UPLOAD_TTL_SECONDS = 3600
CONTENT_TYPE = "application/gzip"
_SHA256_LEN = 64
#: Originals a steward may ask about in one request (the CLI sends batches).
ORIGINALS_PER_REQUEST = 1000
#: S3's limit for a single PUT.
ORIGINAL_MAX_BYTES = 5 * 1024**3
#: How long an `original` download link works (canopy_kb.research.LINK_MINUTES says so).
ORIGINAL_LINK_SECONDS = 15 * 60


class KbNotConfigured(RuntimeError):
    """No bucket to hold knowledge bases on this deployment."""


class KbPublishError(ValueError):
    """A publish that cannot be done (wrong key, missing object, bad hash)."""


def _bucket() -> str:
    bucket = getattr(settings, "CANOPY_KB_BUCKET", "") or getattr(settings, "CANOPY_ATTACHMENTS_BUCKET", "")
    if not bucket:
        raise KbNotConfigured("no CANOPY_KB_BUCKET (or CANOPY_ATTACHMENTS_BUCKET) is set")
    return bucket


def _client():
    """SigV4 always: an original's PUT then signs its `x-amz-checksum-sha256` header, so S3
    refuses bytes that are not the sha256 they are stored under (SigV2, which botocore can
    still pick for presigning, would leave the checksum unsigned)."""
    import boto3
    from botocore.config import Config

    return boto3.client("s3", config=Config(signature_version="s3v4"))


def root(project) -> str:
    """Where everything the project's knowledge base stores lives: keyed by the project,
    never by its address, so a rename (`services.promote_project`) moves nothing (#2047)."""
    return project.storage_root or f"kb-projects/{project.pk}/"


def _prefix(project) -> str:
    return f"{root(project)}kb/"


def _originals_prefix(project) -> str:
    return f"{root(project)}originals/"


def _is_sha256(v: str) -> bool:
    return len(v) == _SHA256_LEN and all(c in "0123456789abcdef" for c in v)


def upload_target(project) -> dict:
    """A presigned PUT for a new kb.sqlite.gz under this project's prefix."""
    key = f"{_prefix(project)}{uuid.uuid4().hex}.sqlite.gz"
    url = _client().generate_presigned_url(
        "put_object", Params={"Bucket": _bucket(), "Key": key, "ContentType": CONTENT_TYPE},
        ExpiresIn=UPLOAD_TTL_SECONDS)
    return {"key": key, "url": url, "headers": {"Content-Type": CONTENT_TYPE},
            "expires_in": UPLOAD_TTL_SECONDS}


def publish(project, key: str, sha256: str):
    """Point the project at an uploaded object and make it serve a KB."""
    sha256 = (sha256 or "").strip().lower()
    if not key.startswith(_prefix(project)) or "/../" in key:
        raise KbPublishError("that object is not one of this project's uploads")
    if not _is_sha256(sha256):
        raise KbPublishError("sha256 must be the 64-hex-digit hash of the uploaded file")
    s3 = _client()
    try:
        head = s3.head_object(Bucket=_bucket(), Key=key)
    except Exception as exc:  # botocore ClientError: 404/403 alike mean "not there for us"
        raise KbPublishError(f"no uploaded object at {key}") from exc
    point(project, key, sha256, int(head.get("ContentLength") or 0))
    return project


def knowledge_base(project, *, create: bool = False, lock: bool = False):
    """The project's knowledge base (its `kb` `ProjectService` row), or None.
    `create` makes it on first publish; `lock` reads it for update."""
    from .models import ProjectService  # cycle: agents.models -> agents.kb_store  # noqa: PLC0415

    qs = ProjectService.objects.filter(project=project, kind=ProjectService.KB).select_related("kb_version")
    if lock:
        # Only this row: PostgreSQL refuses FOR UPDATE on the nullable side of the
        # kb_version outer join.
        qs = qs.select_for_update(of=("self",))
    kb = qs.first()
    if kb is None and create:
        kb, _ = ProjectService.objects.get_or_create(project=project, kind=ProjectService.KB, name="kb",
                                                     defaults={"description": project.outcome or ""})
    project._knowledge_base = kb
    return kb


def point(project, key: str, sha256: str, size: int, version=None) -> None:
    """Serve `key` from now on (every instance re-fetches by its sha256). The previous
    object stays: it is an earlier version's file, pruned by `kb_versions.prune`."""
    kb = knowledge_base(project, create=True)
    kb.kb_key, kb.kb_sha256, kb.kb_bytes = key, sha256, size
    kb.kb_published_at = timezone.now()
    fields = ["kb_key", "kb_sha256", "kb_bytes", "kb_published_at", "updated_at"]
    if version is not None:
        kb.kb_version = version
        fields.append("kb_version")
    kb.save(update_fields=fields)
    project.save(update_fields=["updated_at"])


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except Exception:  # noqa: BLE001 - a stray old object costs storage, not correctness
        pass


# ---- originals ----

#: HEADs in flight while checking a batch of originals (1000 per request at most).
ORIGINALS_HEAD_WORKERS = 32


def _stored_originals(project, shas: set[str]) -> set[str]:
    return _stored(_originals_prefix(project), shas)


def _stored(prefix: str, shas: set[str]) -> set[str]:
    """Which of `shas` are stored under `prefix`, by a HEAD each. Never a listing: the
    task role may read and write objects but not list the bucket, and without list S3
    answers a missing object 403 rather than 404, so any error counts as not stored
    (the cost is one re-upload, never a missing file)."""
    import concurrent.futures as cf

    bucket, s3 = _bucket(), _client()

    def stored(sha: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=prefix + sha)
            return True
        except Exception:  # noqa: BLE001 - 403/404 alike: upload it
            return False

    with cf.ThreadPoolExecutor(ORIGINALS_HEAD_WORKERS) as ex:
        found = dict(zip(shas, ex.map(stored, shas)))
    return {sha for sha, ok in found.items() if ok}


def originals_upload(project, files: list[dict]) -> dict:
    """Presigned PUTs for the originals in `files` ({sha256, bytes, content_type}) that this
    project's store does not hold yet. Each PUT must carry its `headers`: S3 refuses bytes
    whose sha256 is not the one in the key."""
    if len(files) > ORIGINALS_PER_REQUEST:
        raise KbPublishError(f"at most {ORIGINALS_PER_REQUEST} originals per request")
    for f in files:
        f["sha256"] = (f.get("sha256") or "").strip().lower()
        if not _is_sha256(f["sha256"]):
            raise KbPublishError(f"not a sha256: {f['sha256'][:80]!r}")
        if not 0 <= int(f.get("bytes") or 0) <= ORIGINAL_MAX_BYTES:
            raise KbPublishError(f"{f['sha256']}: originals are at most {ORIGINAL_MAX_BYTES} bytes")
    bucket, prefix, s3 = _bucket(), _originals_prefix(project), _client()
    have = _stored_originals(project, {f["sha256"] for f in files})
    uploads, asked = [], set()
    for f in files:
        sha = f["sha256"]
        if sha in have or sha in asked:
            continue
        asked.add(sha)
        ctype = (f.get("content_type") or "application/octet-stream")[:200]
        checksum = base64.b64encode(bytes.fromhex(sha)).decode()
        url = s3.generate_presigned_url(
            "put_object", Params={"Bucket": bucket, "Key": prefix + sha, "ContentType": ctype,
                                  "ChecksumSHA256": checksum}, ExpiresIn=UPLOAD_TTL_SECONDS)
        uploads.append({"sha256": sha, "url": url,
                        "headers": {"Content-Type": ctype, "x-amz-checksum-sha256": checksum}})
    return {"have": len({f["sha256"] for f in files} - asked), "uploads": uploads,
            "expires_in": UPLOAD_TTL_SECONDS}


def original_link(project):
    """The `link` the `original` tool calls (`canopy_kb.research.OriginalLink`): a
    download of one stored original under its file name, or None when it is not stored."""
    bucket, prefix, s3 = _bucket(), _originals_prefix(project), _client()

    def link(sha256: str, filename: str, content_type: str) -> str | None:
        if not _is_sha256(sha256):
            return None
        try:
            s3.head_object(Bucket=bucket, Key=prefix + sha256)
        except Exception:  # noqa: BLE001 - not stored (or not ours): the tool says so
            return None
        name = (filename or sha256).replace('"', "")
        return s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": prefix + sha256,
                                  "ResponseContentType": content_type or "application/octet-stream",
                                  "ResponseContentDisposition": f"attachment; filename*=UTF-8''{quote(name)}"},
            ExpiresIn=ORIGINAL_LINK_SECONDS)

    return link


# ---- curation: step results and the curation state (`canopy_kb.curate`) ----

#: Where each kind of stored object lives. `results` are a curation's step results (an
#: extracted text, an OCR transcription, the curation state itself), gzipped under
#: their step key; `records` and `originals` are the ones above.
RESULTS, ORIGINALS = "results", "originals"
#: How long a steward's download link works (a refresh fetches as it goes).
STEWARD_LINK_SECONDS = UPLOAD_TTL_SECONDS


def _results_prefix(project) -> str:
    return f"{root(project)}results/"


def _store_prefix(project, store: str) -> str:
    prefixes = {RESULTS: _results_prefix, ORIGINALS: _originals_prefix}
    if store not in prefixes:
        raise KbPublishError(f"store is one of {sorted(prefixes)}, not {store!r}")
    return prefixes[store](project)


#: Objects a steward may ask about, or upload, in one request.
BLOBS_PER_REQUEST = 1000


def _keys(values: list[str]) -> list[str]:
    if len(values) > BLOBS_PER_REQUEST:
        raise KbPublishError(f"at most {BLOBS_PER_REQUEST} keys per request")
    out = [(v or "").strip().lower() for v in values]
    for v in out:
        if not _is_sha256(v):
            raise KbPublishError(f"not a key (a sha256): {v[:80]!r}")
    return out


def stored(project, store: str, keys: list[str]) -> list[str]:
    """Which of `keys` the project's `store` holds."""
    return sorted(_stored(_store_prefix(project, store), set(_keys(keys))))


def results_upload(project, blobs: list[dict]) -> dict:
    """Presigned PUTs for step results ({sha256: the step key, gz_sha256 and bytes: of the
    gzip uploaded}) the store lacks. A key names its inputs, not its bytes, so a stored
    result is never replaced: the same key is the same result."""
    for b in blobs:
        if not 0 < int(b.get("bytes") or 0) <= ORIGINAL_MAX_BYTES:
            raise KbPublishError(f"{b.get('sha256')}: a step result is at most {ORIGINAL_MAX_BYTES} bytes")
    return _presigned_puts(project, _results_prefix(project), blobs)


def _presigned_puts(project, prefix: str, blobs: list[dict]) -> dict:
    _keys([b.get("sha256") for b in blobs])
    for b in blobs:
        for f in ("sha256", "gz_sha256"):
            b[f] = (b.get(f) or "").strip().lower()
            if not _is_sha256(b[f]):
                raise KbPublishError(f"{f} is not a sha256: {b[f][:80]!r}")
    bucket, s3 = _bucket(), _client()
    have = _stored(prefix, {b["sha256"] for b in blobs})
    uploads, asked = [], set()
    for b in blobs:
        if b["sha256"] in have or b["sha256"] in asked:
            continue
        asked.add(b["sha256"])
        checksum = base64.b64encode(bytes.fromhex(b["gz_sha256"])).decode()
        url = s3.generate_presigned_url(
            "put_object", Params={"Bucket": bucket, "Key": prefix + b["sha256"], "ContentType": CONTENT_TYPE,
                                  "ChecksumSHA256": checksum}, ExpiresIn=UPLOAD_TTL_SECONDS)
        uploads.append({"sha256": b["sha256"], "url": url,
                        "headers": {"Content-Type": CONTENT_TYPE, "x-amz-checksum-sha256": checksum}})
    return {"have": len({b["sha256"] for b in blobs} - asked), "uploads": uploads,
            "expires_in": UPLOAD_TTL_SECONDS}


def links(project, store: str, keys: list[str]) -> dict:
    """Presigned GETs for stored objects: a result or record comes gzipped, an original
    as itself. A key not stored gets a link that answers 403/404."""
    prefix, bucket, s3 = _store_prefix(project, store), _bucket(), _client()
    out = [{"key": k, "url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": prefix + k},
                                                         ExpiresIn=STEWARD_LINK_SECONDS)}
           for k in _keys(keys)]
    return {"links": out, "expires_in": STEWARD_LINK_SECONDS}


# ---- the serving copy ----

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _cache_root() -> Path:
    return Path(getattr(settings, "CANOPY_KB_CACHE", "") or Path(tempfile.gettempdir()) / "canopy-kb")


def _lock(name: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def local_path(project) -> Path:
    """The decompressed kb.sqlite for the project's CURRENT publish, fetched once
    per instance. Older copies of the same project are removed."""
    kb = project.knowledge_base
    if kb is None or not kb.kb_key or not kb.kb_sha256:
        raise KbPublishError("this project has not published a knowledge base")
    folder = _cache_root() / f"project-{project.pk}"
    path = folder / f"{kb.kb_sha256}.sqlite"
    if path.exists():
        return path
    with _lock(str(path)):
        if path.exists():
            return path
        folder.mkdir(parents=True, exist_ok=True)
        gz = folder / f".{kb.kb_sha256}.gz.part"
        digest = hashlib.sha256()
        body = _client().get_object(Bucket=_bucket(), Key=kb.kb_key)["Body"]
        with open(gz, "wb") as out:
            for chunk in iter(lambda: body.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != kb.kb_sha256:
            gz.unlink(missing_ok=True)
            raise KbPublishError("the stored knowledge base does not match its published hash")
        part = folder / f".{kb.kb_sha256}.sqlite.part"
        with gzip.open(gz, "rb") as src, open(part, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        gz.unlink(missing_ok=True)
        part.replace(path)
        for old in folder.glob("*.sqlite"):
            if old != path:
                old.unlink(missing_ok=True)
    return path


def file_link(project) -> dict:
    """A 15-minute download of the kb.sqlite.gz the project serves now (`canopy kb pull`)."""
    kb = project.knowledge_base
    if kb is None or not kb.kb_key:
        raise KbPublishError("this project has not published a knowledge base")
    url = _client().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": kb.kb_key}, ExpiresIn=ORIGINAL_LINK_SECONDS)
    return {"url": url, "sha256": kb.kb_sha256, "bytes": kb.kb_bytes or 0,
            "version": kb.kb_version_number, "published_at": kb.kb_published_at}


def open_kb(project):
    from canopy_kb import KB

    return KB(local_path(project))
