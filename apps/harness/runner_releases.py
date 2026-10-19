"""Built runner releases — where the installers and updates for a runner version live.

CI builds every runner-source sha that reaches main and uploads, under
`s3://<CANOPY_RUNNER_RELEASES_BUCKET>/<RUNNER_RELEASES_PREFIX><sha>/`, the
artifacts and then `manifest.json` (spec 2026-10-19-runner-installers,
"Releases"). What is CURRENT is the sha this deployment's image names
(`settings.RUNNER_CODE_SHA`); a sha with no manifest has no release.

The bucket is private. canopy-web decides who may download (a signed-in person,
or a runner with its own token) and answers with a presigned URL good for 15
minutes, so ~100 MB installers never stream through the app.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

#: Release platforms, and how a person reads them.
PLATFORMS: dict[str, str] = {
    "macos-arm64": "macOS (Apple silicon)",
    "macos-x86_64": "macOS (Intel)",
    "windows-x86_64": "Windows",
}
PRESIGN_SECONDS = 900
_HIT_TTL = 60
_MISS_TTL = 15
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


class NotConfigured(RuntimeError):
    """No releases bucket: downloads are off on this deployment."""


def bucket() -> str:
    name = getattr(settings, "CANOPY_RUNNER_RELEASES_BUCKET", "") or ""
    if not name:
        raise NotConfigured("CANOPY_RUNNER_RELEASES_BUCKET is not set")
    return name


def prefix() -> str:
    return getattr(settings, "RUNNER_RELEASES_PREFIX", "runner-releases/") or ""


def deployed_sha() -> str:
    return (getattr(settings, "RUNNER_CODE_SHA", "") or "").strip()


def _client():
    # Lazy, like attachment_storage: importable with no boto3 and no AWS.
    import boto3

    return boto3.client("s3")


def _read_manifest(sha: str) -> dict | None:
    import json

    try:
        obj = _client().get_object(Bucket=bucket(), Key=f"{prefix()}{sha}/manifest.json")
        data = json.loads(obj["Body"].read())
    except NotConfigured:
        raise
    except Exception as exc:  # NoSuchKey, access, a malformed file: no release
        logger.info("runner release manifest for %s unreadable: %s", sha, exc)
        return None
    return data if isinstance(data, dict) else None


def manifest(sha: str) -> dict | None:
    """The release manifest for `sha`, or None when none is built (cached
    briefly either way, so a download page does not hit S3 per view)."""
    if not sha:
        return None
    bucket()  # NotConfigured before anything is cached
    key = f"runner-release-manifest:{sha}"
    hit = cache.get(key)
    if hit is not None:
        return hit.get("m")
    found = _read_manifest(sha)
    cache.set(key, {"m": found}, timeout=_HIT_TTL if found else _MISS_TTL)
    return found


def artifact(man: dict | None, platform: str, which: str) -> dict | None:
    """`{file, sha256, size, signed, notarized}` for the platform's `installer`
    or `update` artifact, or None when the manifest has none (or a bad one)."""
    entry = ((man or {}).get("artifacts") or {}).get(platform)
    if not isinstance(entry, dict):
        return None
    art = entry.get(which)
    if not isinstance(art, dict):
        return None
    name = str(art.get("file") or "")
    if not _SAFE_FILE.match(name):
        return None
    try:
        size = int(art.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {"file": name, "sha256": str(art.get("sha256") or ""), "size": size,
            "signed": bool(entry.get("signed")), "notarized": bool(entry.get("notarized"))}


def presign(sha: str, file: str, *, expires: int = PRESIGN_SECONDS) -> str:
    """A GET URL for one artifact of `sha`, downloaded under its own name."""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket(), "Key": f"{prefix()}{sha}/{file}",
                "ResponseContentDisposition": f'attachment; filename="{file}"'},
        ExpiresIn=expires,
    )


def download_href(platform: str) -> str:
    return f"/download/runner/{platform}"
