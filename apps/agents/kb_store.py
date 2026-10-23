"""A workspace project's published knowledge base — where the bytes live and the
copy each serving instance reads.

The steward builds `kb.sqlite` on its own machine (`canopy kb build`) and
publishes it here; canopy-web then answers the project's MCP tools from it
(`apps/mcp/projects.py`). Readers never download anything: they call the tools.

* **Upload** goes straight to S3 on a presigned PUT (`upload_target`), never
  through the web dyno — a KB is a few hundred MB compressed.
* **Publish** (`publish`) checks the object is there, under THIS project's
  prefix, then points the project at it (`point`). The previous object stays, as
  an earlier version's file (`kb_versions`, which prunes beyond the newest few).
* **Records** (`blobs_upload`, `get_blob`, `put_blob`): a delta publish's document
  records and corpus parts (`canopy_kb.records`), gzipped canonical JSON under
  `records/<sha256 of the JSON>`; the index job (`kb_index`) reads them, checked
  against their address, and stores what it builds (`upload_file`, `adopt`).
* **Read** (`open_kb`): each instance keeps the decompressed copy on local disk,
  keyed by the published object's sha256, so a new publish is picked up on the
  next call and an unchanged one is never fetched twice.
* **Originals** (`originals_upload`, `original_link`): the files each document's text
  was read from (PDFs, spreadsheets, pages), stored under the project's
  `originals/<sha256>`. The steward uploads only what the store lacks, on presigned
  PUTs that S3 checks against the sha256; the `original` tool hands a reader a
  15-minute download link. Never deleted: a file the publisher changed or removed is
  the version a KB's `originals` table still lists, and the reason to keep originals.
* **Working cache** (`workdir_*`, `canopy kb workdir save|restore`): what a curator's
  refresh keeps between runs (fetched bytes with their ETags, extracted text, OCR,
  catalog cards), saved here after a publish so the next refresh can run on any
  machine. Files are stored by sha256 under `workdir/` (gzipped), except those the
  originals already hold; a manifest lists them and `project.kb_workdir` points at it.

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

from canopy_kb import records
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


def _prefix(project) -> str:
    return f"workspace-projects/{project.owner_workspace_id}/{project.handle}/kb/"


def _originals_prefix(project) -> str:
    return f"workspace-projects/{project.owner_workspace_id}/{project.handle}/originals/"


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


def point(project, key: str, sha256: str, size: int, version=None) -> None:
    """Serve `key` from now on (every instance re-fetches by its sha256). The previous
    object stays: it is an earlier version's file, pruned by `kb_versions.prune`."""
    project.mcp_kind = project.MCP_KB
    project.kb_key, project.kb_sha256, project.kb_bytes = key, sha256, size
    project.kb_published_at = timezone.now()
    fields = ["mcp_kind", "kb_key", "kb_sha256", "kb_bytes", "kb_published_at", "updated_at"]
    if version is not None:
        project.kb_version = version
        fields.append("kb_version")
    project.save(update_fields=fields)


def delete_object(key: str) -> None:
    try:
        _client().delete_object(Bucket=_bucket(), Key=key)
    except Exception:  # noqa: BLE001 - a stray old object costs storage, not correctness
        pass


def upload_file(project, path: Path) -> tuple[str, str, int]:
    """Store a kb.sqlite.gz canopy-web built itself (the index job). Returns its key,
    sha256 and size."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    key = f"{_prefix(project)}{uuid.uuid4().hex}.sqlite.gz"
    _client().upload_file(str(path), _bucket(), key, ExtraArgs={"ContentType": CONTENT_TYPE})
    return key, digest.hexdigest(), path.stat().st_size


# ---- document records and parts (`canopy_kb.records`), content-addressed ----

#: Records and parts a steward may ask about in one request.
BLOBS_PER_REQUEST = 1000
#: A record holds one document's text: the largest GiveWell one is a few MB.
BLOB_MAX_BYTES = 256 * 1024**2


def _blobs_prefix(project) -> str:
    return f"workspace-projects/{project.owner_workspace_id}/{project.handle}/records/"


def blobs_upload(project, blobs: list[dict]) -> dict:
    """Presigned PUTs for the records and parts in `blobs` ({sha256: of the canonical
    JSON, gz_sha256 and bytes: of the gzip the steward uploads}) that the store lacks.
    S3 checks the gzip's sha256; the index job checks the content's when it reads one."""
    if len(blobs) > BLOBS_PER_REQUEST:
        raise KbPublishError(f"at most {BLOBS_PER_REQUEST} records per request")
    for b in blobs:
        for f in ("sha256", "gz_sha256"):
            b[f] = (b.get(f) or "").strip().lower()
            if not _is_sha256(b[f]):
                raise KbPublishError(f"{f} is not a sha256: {b[f][:80]!r}")
        if not 0 < int(b.get("bytes") or 0) <= BLOB_MAX_BYTES:
            raise KbPublishError(f"{b['sha256']}: a record is at most {BLOB_MAX_BYTES} bytes")
    bucket, prefix, s3 = _bucket(), _blobs_prefix(project), _client()
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


def missing_blobs(project, shas: set[str]) -> set[str]:
    return set(shas) - _stored(_blobs_prefix(project), set(shas))


def get_blob(project, sha: str):
    """A record or part, checked against its address."""
    body = _client().get_object(Bucket=_bucket(), Key=_blobs_prefix(project) + sha)["Body"].read()
    return records.decode(body, sha)


def put_blob(project, obj) -> str:
    """Store a part canopy-web makes itself (a version's state). Returns its sha256."""
    sha = records.sha(obj)
    _client().put_object(Bucket=_bucket(), Key=_blobs_prefix(project) + sha, Body=records.encode(obj),
                         ContentType=CONTENT_TYPE)
    return sha


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


# ---- the curator's working cache ----

#: Files a steward may ask about in one request (the CLI sends batches).
WORKDIR_PER_REQUEST = 1000
#: How long a working-cache download link works (a restore fetches as it goes).
WORKDIR_LINK_SECONDS = UPLOAD_TTL_SECONDS
WORKDIR = "workdir"
ORIGINALS = "originals"


def _workdir_prefix(project) -> str:
    return f"workspace-projects/{project.owner_workspace_id}/{project.handle}/workdir/"


def _shas(values, limit: int = WORKDIR_PER_REQUEST) -> list[str]:
    if len(values) > limit:
        raise KbPublishError(f"at most {limit} files per request")
    out = [(v or "").strip().lower() for v in values]
    for v in out:
        if not _is_sha256(v):
            raise KbPublishError(f"not a sha256: {v[:80]!r}")
    return out


def workdir_have(project, shas: list[str]) -> dict:
    """Where each of `shas` (a file's content sha256) is stored already: among the
    originals (a fetched PDF, page or workbook is the same bytes `push --originals`
    stored), or in the working cache. A file in neither is the steward's to upload."""
    wanted = set(_shas(shas))
    originals = _stored_originals(project, wanted)
    workdir = _stored(_workdir_prefix(project), wanted - originals)
    return {ORIGINALS: sorted(originals), WORKDIR: sorted(workdir)}


def workdir_upload(project, blobs: list[dict]) -> dict:
    """Presigned PUTs for working-cache files ({sha256: of the file, gz_sha256 and
    bytes: of the gzip uploaded}) that the cache lacks. Stored gzipped under the file's
    own sha256, like a record (S3 checks the gzip's; a restore checks the file's)."""
    _shas([b.get("sha256") for b in blobs])
    for b in blobs:
        for f in ("sha256", "gz_sha256"):
            b[f] = (b.get(f) or "").strip().lower()
            if not _is_sha256(b[f]):
                raise KbPublishError(f"{f} is not a sha256: {b[f][:80]!r}")
        if not 0 < int(b.get("bytes") or 0) <= ORIGINAL_MAX_BYTES:
            raise KbPublishError(f"{b['sha256']}: a working-cache file is at most {ORIGINAL_MAX_BYTES} bytes")
    bucket, prefix, s3 = _bucket(), _workdir_prefix(project), _client()
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


def workdir_links(project, files: list[dict]) -> dict:
    """Presigned GETs for working-cache files ({sha256, store}): a `workdir` one is the
    gzip `workdir_upload` stored, an `originals` one the file itself."""
    _shas([f.get("sha256") for f in files])
    bucket, s3 = _bucket(), _client()
    prefixes = {WORKDIR: _workdir_prefix(project), ORIGINALS: _originals_prefix(project)}
    links = []
    for f in files:
        store = f.get("store") or WORKDIR
        if store not in prefixes:
            raise KbPublishError(f"store is {WORKDIR!r} or {ORIGINALS!r}, not {store!r}")
        sha = f["sha256"].strip().lower()
        url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": prefixes[store] + sha},
                                        ExpiresIn=WORKDIR_LINK_SECONDS)
        links.append({"sha256": sha, "store": store, "url": url})
    return {"links": links, "expires_in": WORKDIR_LINK_SECONDS}


def workdir_save(project, manifest: str, base: str | None, files: int, size: int, saved_by: str) -> dict:
    """Point the project at a saved working cache: `manifest` is the sha256 of its
    manifest, uploaded first as a working-cache file. `base` is the manifest the saver
    restored from (None for the first save): another machine saving since is a
    conflict (`KbWorkdirConflict`), so a cache is never silently replaced by an older one."""
    (manifest,) = _shas([manifest])
    current = (project.kb_workdir or {}).get("manifest")
    if current and current != manifest and (base or "").strip().lower() != current:
        raise KbWorkdirConflict(f"the working cache was saved since ({current[:12]}); restore it, then save again")
    if not _stored(_workdir_prefix(project), {manifest}):
        raise KbPublishError(f"no working-cache manifest {manifest[:12]} uploaded")
    project.kb_workdir = {"manifest": manifest, "files": int(files), "bytes": int(size),
                          "saved_at": timezone.now().isoformat(), "saved_by": saved_by,
                          "version": project.kb_version_number}
    project.save(update_fields=["kb_workdir", "updated_at"])
    return project.kb_workdir


class KbWorkdirConflict(ValueError):
    """Another machine saved the working cache since this one restored it."""


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
    if not project.kb_key or not project.kb_sha256:
        raise KbPublishError("this project has not published a knowledge base")
    folder = _cache_root() / f"{project.owner_workspace_id}--{project.handle}"
    path = folder / f"{project.kb_sha256}.sqlite"
    if path.exists():
        return path
    with _lock(str(path)):
        if path.exists():
            return path
        folder.mkdir(parents=True, exist_ok=True)
        gz = folder / f".{project.kb_sha256}.gz.part"
        digest = hashlib.sha256()
        body = _client().get_object(Bucket=_bucket(), Key=project.kb_key)["Body"]
        with open(gz, "wb") as out:
            for chunk in iter(lambda: body.read(1 << 20), b""):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != project.kb_sha256:
            gz.unlink(missing_ok=True)
            raise KbPublishError("the stored knowledge base does not match its published hash")
        part = folder / f".{project.kb_sha256}.sqlite.part"
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
    if not project.kb_key:
        raise KbPublishError("this project has not published a knowledge base")
    url = _client().generate_presigned_url(
        "get_object", Params={"Bucket": _bucket(), "Key": project.kb_key}, ExpiresIn=ORIGINAL_LINK_SECONDS)
    return {"url": url, "sha256": project.kb_sha256, "bytes": project.kb_bytes or 0,
            "version": project.kb_version_number, "published_at": project.kb_published_at}


def adopt(project, sha256: str, built: Path) -> None:
    """Make a file this instance built (the index job) its serving copy for `sha256`."""
    folder = _cache_root() / f"{project.owner_workspace_id}--{project.handle}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sha256}.sqlite"
    with _lock(str(path)):
        if not path.exists():
            Path(built).replace(path)
        for old in folder.glob("*.sqlite"):
            if old != path:
                old.unlink(missing_ok=True)


def open_kb(project):
    from canopy_kb import KB

    return KB(local_path(project))
