"""Storage layer over the Drive client.

Drive layout (under CANOPY_DRIVE_ROOT_FOLDER_ID):
    walkthroughs/
        <walkthrough-uuid>/
            slideshow.html  OR  video.mp4

Each walkthrough gets its own subfolder so deletion is a clean
folder-drop and Drive UI stays browseable.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.walkthroughs.drive_client import DriveNotConfigured, get_drive_client

WALKTHROUGHS_FOLDER = "walkthroughs"
# A DDD narrative's iteration sources (apps.walkthroughs.models.NarrativeSource):
# narrative-sources/<source-uuid>/<role filename>.
NARRATIVE_SOURCES_FOLDER = "narrative-sources"


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    folder_id: str


def _require_root() -> str:
    root = getattr(settings, "CANOPY_DRIVE_ROOT_FOLDER_ID", "") or ""
    if not root:
        raise DriveNotConfigured("CANOPY_DRIVE_ROOT_FOLDER_ID is empty")
    return root


def store_upload(
    *,
    walkthrough_id: str,
    filename: str,
    content_type: str,
    data: bytes,
    folder: str = WALKTHROUGHS_FOLDER,
) -> StoredFile:
    """Upload bytes under <folder>/<id>/<filename>; return ids."""
    root = _require_root()
    client = get_drive_client()
    parent = client.find_or_create_folder(folder, root)
    subfolder = client.find_or_create_folder(str(walkthrough_id), parent)
    file_id = client.upload(
        parent_id=subfolder,
        name=filename,
        content_type=content_type,
        data=data,
    )
    return StoredFile(file_id=file_id, folder_id=subfolder)


def download(
    *, file_id: str, start: int | None = None, end: int | None = None
) -> tuple[bytes, int, int, int]:
    """Pass-through to the underlying Drive client. Returns
    (data, start, end_inclusive, total)."""
    client = get_drive_client()
    return client.download(file_id, start=start, end=end)


def delete_stored(*, file_id: str, folder_id: str) -> None:
    """Trash the file AND its per-walkthrough subfolder.

    Each walkthrough lives in its own subfolder so trashing both makes the
    Drive listing actually look empty after the user deletes a walkthrough.
    (Previously we left the folder in place; in practice it accumulated
    empty UUID-named dirs that an operator had to sweep by hand.)
    """
    client = get_drive_client()
    client.delete(file_id)
    if folder_id:
        client.delete(folder_id)
