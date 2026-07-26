"""S3 for chat attachment bytes — the only module that knows where they live.

Two readers need the same bytes: the browser (rendering a thumbnail inline) and
the runner (downloading into the agent's workspace so the agent can open the
file). Both go through the server rather than talking to S3 directly, which is
what keeps the bucket private with no CORS rules, no presigned-URL expiry to
reason about, and one place enforcing "is this yours to read".

Access in prod is granted by a BUCKET policy naming the ECS task role, because
that role belongs to the shared labs platform and is only a parameter to our
stack — we cannot attach an IAM policy to it. Credentials therefore come from
the task role's ambient environment; boto3 finds them itself and this module
never handles a key.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from django.conf import settings


class AttachmentsNotConfigured(RuntimeError):
    """No bucket configured. Raised rather than silently degraded: writing a row
    that points at bytes which were never stored is worse than a clear 503."""


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    content_type: str


def is_configured() -> bool:
    return bool(getattr(settings, "CANOPY_ATTACHMENTS_BUCKET", ""))


def _bucket() -> str:
    bucket = getattr(settings, "CANOPY_ATTACHMENTS_BUCKET", "")
    if not bucket:
        raise AttachmentsNotConfigured("CANOPY_ATTACHMENTS_BUCKET is not set")
    return bucket


def _client():
    # Imported lazily so the module (and every test that touches models) stays
    # importable on a machine with no boto3 wheel and no AWS anything.
    import boto3

    return boto3.client("s3")


def safe_filename(name: str) -> str:
    """A filename safe to put in an S3 key and to echo back in a header.

    Attacker-controlled: it arrives from a multipart upload. Path separators are
    the real hazard (`../` climbing out of the session prefix), so the basename
    is taken and anything exotic is dropped rather than escaped — a screenshot
    losing an unusual character is a better outcome than a key we cannot reason
    about. Never returns empty.
    """
    name = unicodedata.normalize("NFKD", name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # basename, both separators
    cleaned = "".join(
        c if (c.isalnum() or c in "._-") else "-" for c in name
    ).strip(".-")
    return cleaned[:120] or "attachment"


def storage_key(session_id, attachment_id, filename: str) -> str:
    """Session-prefixed so a bucket listing is browsable and a whole session's
    bytes can be dropped with one prefix delete. The attachment id makes it
    unique — two screenshots really are both called `IMG_0001.png`."""
    return f"sessions/{session_id}/{attachment_id}/{safe_filename(filename)}"


def put(key: str, body: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=_bucket(), Key=key, Body=body, ContentType=content_type,
    )


def get(key: str) -> StoredObject:
    obj = _client().get_object(Bucket=_bucket(), Key=key)
    return StoredObject(
        body=obj["Body"].read(),
        content_type=obj.get("ContentType", "application/octet-stream"),
    )


def delete(key: str) -> None:
    """Best-effort: a missing object is already the desired state, and S3's
    delete is idempotent, so this never raises on "not there"."""
    _client().delete_object(Bucket=_bucket(), Key=key)
