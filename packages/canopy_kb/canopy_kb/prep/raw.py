"""The raw HTTP cache every stage reads, and the small file helpers they share.

A curator's adapters fetch through `Raw`, so every response is kept byte for byte:
extraction can be redone offline, a refresh refetches only what can change, and the
same bytes are what `originals` publishes beside the knowledge base.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

UA = "canopy-kb/0.2 (research)"


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(s: str | bytes) -> str:
    return hashlib.sha256(s.encode() if isinstance(s, str) else s).hexdigest()


def read_json(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]


def write_jsonl(p: Path, rows) -> int:
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(p)
    return n


#: What a Google Docs export regenerates every time: its stylesheet, class names, and the
#: timestamp and signature in each redirect link (`&ust=…&usg=…`).
_GDOC_VOLATILE = re.compile(rb'<style[^>]*>.*?</style>|\sclass="[^"]*"|&(?:amp;)?(?:ust|usg)=[^&"]*', re.S)


def _stable(url: str, ctype: str, body: bytes) -> bytes | None:
    """The part of an export that changes only when its content does, or None when the
    whole body is that. Google re-exports an unchanged Sheet with new comment-shape VML
    and an unchanged Doc with reshuffled CSS class names and re-stamped links (measured
    2026-10-22), so
    comparing bytes would call every sheet and doc changed on every refresh."""
    if body[:2] == b"PK" and "spreadsheetml" in ctype:
        try:
            z = zipfile.ZipFile(io.BytesIO(body))
            h = hashlib.sha256()
            for name in sorted(z.namelist()):
                if not name.startswith("xl/drawings/vmlDrawing"):
                    h.update(name.encode() + b"\0" + z.read(name))
            return h.digest()
        except zipfile.BadZipFile:
            return None
    if "html" in ctype and "docs.google.com/document" in url:
        return hashlib.sha256(_GDOC_VOLATILE.sub(b"", body)).digest()
    return None


def same_content(url: str, ctype: str, old: bytes, new: bytes) -> bool:
    """Whether a refetched export says the same as the copy we keep."""
    if not old:
        return False
    a, b = _stable(url, ctype, old), _stable(url, ctype, new)
    return a is not None and a == b


class Raw:
    """Every HTTP response kept as bytes.

      <data>/raw/http/<sha1(url)>   body
      <data>/raw/http.jsonl         one line per fetch: url, final_url, status, ctype, cd,
                                    file, bytes, sha256, fetched_at (the last line per url wins)
    """

    KEEP = (200, 401, 403, 404, 410)

    def __init__(self, data: Path, ua: str = UA):
        self.ua = ua
        self.dir = data / "raw" / "http"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = data / "raw" / "http.jsonl"
        self.index: dict[str, dict] = {r["url"]: r for r in read_jsonl(self.index_path)}
        self._lock = threading.Lock()
        self._local = threading.local()

    def _client(self):
        import httpx
        if not hasattr(self._local, "c"):
            self._local.c = httpx.Client(timeout=120, headers={"User-Agent": self.ua}, follow_redirects=True)
        return self._local.c

    def get(self, url: str, refetch: bool = False, ua: str | None = None) -> dict:
        """Cached GET with retries; returns the index row (bytes via `body`).

        A refetch is conditional: a kept response's ETag and Last-Modified go back as
        If-None-Match / If-Modified-Since, and a 304 keeps the row (and its bytes, so
        nothing downstream re-extracts, re-OCRs or re-cards), noting `checked_at`."""
        old = self.index.get(url, {})
        if not refetch and old.get("status") in self.KEEP:
            return old
        cond = {}
        if old.get("status") == 200 and old.get("file") and (self.dir / old["file"]).exists():
            if old.get("etag"):
                cond["If-None-Match"] = old["etag"]
            if old.get("last_modified"):
                cond["If-Modified-Since"] = old["last_modified"]
        row: dict = {"url": url}
        for attempt in range(5):
            try:
                r = self._client().get(url, headers={**({"User-Agent": ua} if ua else {}), **cond} or None)
                if r.status_code == 304 and cond:
                    row = {**old, "checked_at": now()}
                    break
                if r.status_code in (429, 500, 502, 503, 504):
                    row = {"url": url, "status": r.status_code}
                    time.sleep(2 ** attempt * 3)
                    continue
                ctype = r.headers.get("content-type", "")
                kept = self.body(old) if old.get("status") == 200 and self.path(old) and self.path(old).exists() else b""
                if kept and kept != r.content and same_content(url, ctype, kept, r.content):
                    # A re-export that differs only in what the exporter regenerates (a
                    # Google Sheet's comment-shape ids, a Doc's class names): keep the old
                    # bytes and row, so nothing downstream is re-read or re-published.
                    row = {**old, "checked_at": now()}
                    break
                f = hashlib.sha1(url.encode()).hexdigest()
                (self.dir / f).write_bytes(r.content)
                row = {"url": url, "final_url": str(r.url), "status": r.status_code,
                       "ctype": ctype, "cd": r.headers.get("content-disposition", ""),
                       "file": f, "bytes": len(r.content), "sha256": sha(r.content), "fetched_at": now()}
                for h, k in (("etag", "etag"), ("last-modified", "last_modified")):
                    if r.headers.get(h):
                        row[k] = r.headers[h]
                if old.get("sha256") == row["sha256"] and old.get("fetched_at"):
                    row["fetched_at"] = old["fetched_at"]  # same bytes: nothing changed, keep the record stable
                    row["checked_at"] = now()
                break
            except Exception as e:  # noqa: BLE001 — a network error is retried, then recorded
                row = {"url": url, "status": -1, "error": str(e)[:200]}
                time.sleep(2 ** attempt * 2)
        with self._lock:
            self.index[url] = row
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return row

    def body(self, row: dict) -> bytes:
        return (self.dir / row["file"]).read_bytes() if row.get("file") else b""

    def path(self, row: dict) -> Path | None:
        return self.dir / row["file"] if row.get("file") else None

    def compact(self) -> None:
        """Rewrite the index with one line per url (it is append-only while fetching)."""
        with self._lock:
            write_jsonl(self.index_path, self.index.values())
