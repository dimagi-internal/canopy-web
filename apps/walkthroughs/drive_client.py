"""Thin Google Drive v3 wrapper for walkthrough storage.

Modeled on ace-web's apps/opps/drive_client.py but trimmed to the four
methods this app needs: find_or_create_folder, upload, download (range-
aware), delete. Authenticates via a service-account JSON in
CANOPY_DRIVE_SA_KEY_JSON; writes happen under CANOPY_DRIVE_ROOT_FOLDER_ID.
"""
from __future__ import annotations

import functools
import io
import json
import logging
import random
import time
from abc import ABC, abstractmethod

from django.conf import settings

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_ATTEMPTS = 3
_RETRY_BASE_DELAY = 0.5

FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_retry(method):
    """Retry transient Drive errors on read methods (3 attempts, expo backoff)."""

    @functools.wraps(method)
    def _wrapped(self, *args, **kwargs):
        from googleapiclient.errors import HttpError  # noqa: PLC0415 — Google client loads only when Drive is used

        last_exc: Exception | None = None
        for attempt in range(1, _RETRY_ATTEMPTS + 1):
            try:
                return method(self, *args, **kwargs)
            except HttpError as exc:
                status = getattr(getattr(exc, "resp", None), "status", None)
                if status not in _RETRYABLE_STATUS or attempt == _RETRY_ATTEMPTS:
                    raise
                delay = _RETRY_BASE_DELAY * (2 ** (attempt - 1))
                delay += random.uniform(0, delay * 0.25)
                log.warning(
                    "drive_retry: %s attempt %d/%d status=%s sleeping %.2fs",
                    method.__name__, attempt, _RETRY_ATTEMPTS, status, delay,
                )
                time.sleep(delay)
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("drive_retry: unreachable")

    return _wrapped


class DriveNotConfigured(RuntimeError):
    """Raised when CANOPY_DRIVE_SA_KEY_JSON is missing or invalid."""


class DriveClient(ABC):
    """Abstract Drive client — production + fake implementations conform."""

    @abstractmethod
    def find_or_create_folder(self, name: str, parent_id: str) -> str: ...

    @abstractmethod
    def upload(
        self,
        *,
        parent_id: str,
        name: str,
        content_type: str,
        data: bytes,
    ) -> str:
        """Upload bytes, return file_id."""

    @abstractmethod
    def download(
        self,
        file_id: str,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> tuple[bytes, int, int, int]:
        """Return (chunk_bytes, start, end_inclusive, total_size). If start
        and end are None, returns the whole file with start=0 and
        end=total-1."""

    @abstractmethod
    def delete(self, file_id: str) -> None: ...


def get_drive_client() -> DriveClient:
    """Return the configured production client. Raises DriveNotConfigured
    if the SA key is unset/invalid."""
    raw = getattr(settings, "CANOPY_DRIVE_SA_KEY_JSON", "") or ""
    if not raw:
        raise DriveNotConfigured("CANOPY_DRIVE_SA_KEY_JSON is empty")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DriveNotConfigured(f"invalid JSON in CANOPY_DRIVE_SA_KEY_JSON: {e}") from e
    return GoogleDriveClient(sa_info=info)


class GoogleDriveClient(DriveClient):
    """Real implementation wrapping googleapiclient.discovery."""

    def __init__(self, sa_info: dict):
        from google.oauth2 import service_account  # noqa: PLC0415 — as above
        from googleapiclient.discovery import build  # noqa: PLC0415 — as above

        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        # cache_discovery=False avoids a noisy warning in containers
        # where the discovery cache dir isn't writable.
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)

    @_drive_retry
    def find_or_create_folder(self, name: str, parent_id: str) -> str:
        q = (
            f"'{parent_id}' in parents and name = '{name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        resp = self._service.files().list(
            q=q,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = resp.get("files", [])
        if files:
            return files[0]["id"]
        meta = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        created = self._service.files().create(
            body=meta,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    def upload(self, *, parent_id, name, content_type, data) -> str:
        from googleapiclient.http import MediaIoBaseUpload  # noqa: PLC0415 — as above

        media = MediaIoBaseUpload(
            io.BytesIO(data), mimetype=content_type, resumable=False
        )
        meta = {"name": name, "parents": [parent_id]}
        created = self._service.files().create(
            body=meta,
            media_body=media,
            fields="id, size",
            supportsAllDrives=True,
        ).execute()
        return created["id"]

    @_drive_retry
    def download(self, file_id, *, start=None, end=None):
        # First, ask Drive for the total size so range math works even
        # when start/end aren't provided.
        meta = self._service.files().get(
            fileId=file_id,
            fields="size",
            supportsAllDrives=True,
        ).execute()
        total = int(meta["size"])
        if start is None:
            start = 0
        if end is None:
            end = total - 1
        end = min(end, total - 1)
        # alt=media + Range header. MediaIoBaseDownload was tempting but
        # rewrites Range per chunk for progressive downloads, clobbering
        # our bounds and returning the whole file. req.execute() runs a
        # single GET that honors the Range we set. 75 MB upper bound on
        # uploads keeps memory pressure reasonable.
        req = self._service.files().get_media(fileId=file_id)
        req.headers["Range"] = f"bytes={start}-{end}"
        data = req.execute()
        return data, start, end, total

    def delete(self, file_id) -> None:
        # Trash (set trashed=True), not permanent delete. Permanent deletion
        # in a Shared Drive requires the Manager role; Content Manager — the
        # role our spec asks operators to grant — can only trash. We
        # discovered this the hard way when /api/walkthroughs/<id>/ DELETE
        # left orphan files in Drive. Trash is also safer: items are
        # recoverable for 30 days.
        self._service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
