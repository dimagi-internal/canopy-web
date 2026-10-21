"""An item's original file: the bytes the publisher served, as fetched.

The text an item carries is a reading copy, made from the original so it can be searched,
read by section and grepped. The original stays the record: `canopy kb push --originals`
uploads each one beside the knowledge base, stored by its sha256 (an unchanged file is
stored once, a changed one is kept beside its earlier versions), and the `original` tool
hands a reader the file itself: the workbook to recompute, the PDF's charts and layout,
or a copy the publisher has since taken down.

`record` is what goes on an item as `original` (contract: canopy docs/knowledge-bases.md).
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

#: Extensions for the content types a publisher serves without a usable file name.
EXT = {"application/pdf": ".pdf", "text/html": ".html", "application/msword": ".doc",
       "application/vnd.ms-excel": ".xls", "text/plain": ".txt", "text/csv": ".csv",
       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
       "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
       "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}


def filename(row: dict, title: str = "") -> str:
    """A file name for the original: the server's (Content-Disposition), else the URL's
    last segment, else the title; with an extension that matches its type."""
    cd = row.get("cd") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="([^"]+)"', cd)
    name = unquote(m.group(1)) if m else unquote(urlparse(row.get("final_url") or row.get("url") or "").path.rsplit("/", 1)[-1])
    if not name or name in ("edit", "export", "uc", "view") or "." not in name and title:
        name = title or name or "original"
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", name).strip()[:150] or "original"
    ctype = (row.get("ctype") or "").split(";")[0].strip()
    ext = EXT.get(ctype) or mimetypes.guess_extension(ctype or "") or ""
    if ext and not name.lower().endswith(ext):
        name += ext
    return name


def record(row: dict, path: Path | None, title: str = "") -> dict | None:
    """The `original` for an item read from this raw fetch, or None when there is no file."""
    if row.get("status") != 200 or not row.get("sha256") or not path:
        return None
    return {"sha256": row["sha256"], "filename": filename(row, title),
            "content_type": (row.get("ctype") or "application/octet-stream").split(";")[0].strip(),
            "bytes": int(row.get("bytes") or 0), "source_url": row.get("url", ""),
            "fetched_at": row.get("fetched_at", ""), "path": str(path)}
