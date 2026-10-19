"""A workspace project's published knowledge base — where the bytes live and the
copy each serving instance reads.

The steward builds `kb.sqlite` on its own machine (`canopy kb build`) and
publishes it here; canopy-web then answers the project's MCP tools from it
(`apps/mcp/projects.py`). Readers never download anything: they call the tools.

* **Upload** goes straight to S3 on a presigned PUT (`upload_target`), never
  through the web dyno — a KB is a few hundred MB compressed.
* **Publish** (`publish`) checks the object is there, under THIS project's
  prefix, then points the project at it. The previous object is deleted.
* **Read** (`open_kb`): each instance keeps the decompressed copy on local disk,
  keyed by the published object's sha256, so a new publish is picked up on the
  next call and an unchanged one is never fetched twice.

The bucket is `CANOPY_KB_BUCKET`, else the attachments bucket (same access: a
bucket policy naming the task role). No bucket → `KbNotConfigured`, a clear 503,
never a half-published project.
"""
from __future__ import annotations

import gzip
import hashlib
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from django.conf import settings
from django.utils import timezone

UPLOAD_TTL_SECONDS = 3600
CONTENT_TYPE = "application/gzip"
_SHA256_LEN = 64


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
    import boto3

    return boto3.client("s3")


def _prefix(project) -> str:
    return f"workspace-projects/{project.owner_workspace_id}/{project.handle}/kb/"


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
    if len(sha256) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in sha256):
        raise KbPublishError("sha256 must be the 64-hex-digit hash of the uploaded file")
    s3 = _client()
    try:
        head = s3.head_object(Bucket=_bucket(), Key=key)
    except Exception as exc:  # botocore ClientError: 404/403 alike mean "not there for us"
        raise KbPublishError(f"no uploaded object at {key}") from exc
    previous = project.kb_key
    project.mcp_kind = project.MCP_KB
    project.kb_key, project.kb_sha256 = key, sha256
    project.kb_bytes = int(head.get("ContentLength") or 0)
    project.kb_published_at = timezone.now()
    project.save(update_fields=["mcp_kind", "kb_key", "kb_sha256", "kb_bytes", "kb_published_at",
                                "updated_at"])
    if previous and previous != key:
        try:
            s3.delete_object(Bucket=_bucket(), Key=previous)
        except Exception:  # noqa: BLE001 - a stray old object costs storage, not correctness
            pass
    return project


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


def open_kb(project):
    from canopy_kb import KB

    return KB(local_path(project))
