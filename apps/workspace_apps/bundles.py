"""Bundle pages: an app page made of static files (HTML, JS, CSS, images, fonts) instead of one
JSX file, so an existing web UI runs in canopy almost unchanged (docs/architecture/workspace-apps.md
§ Bundle pages).

* **Upload is a plain list.** `[{path, content_base64 | text | sha256}]`, the same for every route
  that releases a template version (`attach`). A file given by `sha256` was sent earlier through
  `POST /api/workspace-apps/{ws}/bundle-blobs` (`store_blobs`), which is how a bundle larger than one
  request body (`DATA_UPLOAD_MAX_MEMORY_SIZE`) arrives: blobs in batches, then the list by hash.
* **The server writes the manifest's `bundle`, never the author.** `attach` drops whatever `bundle`
  a raw manifest carries and sets `{id, size, files}` from the files it stored, so a manifest cannot
  point at another app's bundle. `manifest.py` checks each bundle page's `entry` is one of `files`.
* **Content-addressed, immutable.** A blob is stored once by sha256 (`AppBundleBlob`); a bundle is
  the mapping {path: sha256} (`AppBundle`), and its id is the hash of that mapping. A new template
  version with changed files is a new id; nothing is ever rewritten, so files are cached for good.
* **Served like the dynamic-view frame** (`apps/dynamic_views/frame.py`): `BundleMiddleware`
  answers `/app-bundles/…` before sessions and auth, so no cookie is set or read; every response
  carries `csp` with `sandbox allow-scripts allow-forms`, so a file is opaque-origin even opened
  top-level. Scripts only from this bundle's own path and canopy's bundle client, no `eval`;
  `connect-src` only this bundle's own files (a page may fetch its JSON or templates, never data);
  everything else goes through the `canopy.*` bridge to the page that framed it. `allow-forms` (with
  `form-action 'none'`) only so a `<form>`'s submit EVENT fires, which existing UIs rely on; nothing
  is ever submitted. Files are readable by anyone who knows the bundle's id — they are code, never
  tenant data, and a bundle must hold no secret.
* **The canopy client is injected, not bundled.** Every HTML file is served with canopy's bundle
  client (`bundle_client.js`, a classic script, so it runs before the page's own) and the origins it
  may talk to, under a per-response nonce. It defines `window.canopy` with the same calls a JSX page
  has, over the same postMessage protocol (`frontend/src/dynamicViews/protocol.ts`).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.urls import get_script_prefix
from django.utils import timezone

from apps.embedding.extension import extension_origins
from apps.mcp_apps_host.views_mcp_apps import host_origins

from .models import AppBundle, AppBundleBlob

PREFIX = "/app-bundles/"
CLIENT_PATH = "/app-bundles/client.js"
CLIENT_FILE = Path(__file__).with_name("bundle_client.js")

MAX_BUNDLE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 1000
MAX_PATH = 200
MAX_DEPTH = 12
#: An uploaded blob no bundle names after this long is swept (`sweep_unused_blobs`).
UNUSED_BLOB_DAYS = 1

#: The only files a bundle may hold, and the type each is served as (never sniffed).
TYPES = {
    "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8", "mjs": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8", "json": "application/json", "map": "application/json",
    "txt": "text/plain; charset=utf-8", "svg": "image/svg+xml", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp", "avif": "image/avif", "ico": "image/x-icon",
    "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "otf": "font/otf",
}
HTML = ("html", "htm")
_SEGMENT = re.compile(r"^[A-Za-z0-9_@+\-][A-Za-z0-9_.@+\-]*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(rb"<head\b[^>]*>", re.I)
_HTML_TAG = re.compile(rb"<html\b[^>]*>", re.I)
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.I)


def _fail(where: str, why: str):
    raise ValueError(f"{where}: {why}")


def ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""


def check_path(where: str, path) -> str:
    """A bundle-relative path: `/`-separated segments of letters, digits and `_ . @ + -`, none
    starting with a dot (so no `..`, no hidden files), of an allowed type."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH:
        _fail(where, f"must be a path of at most {MAX_PATH} characters")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH or not all(_SEGMENT.match(p) for p in parts):
        _fail(where, f"'{path}' must be relative, '/'-separated, with no empty, '.' or '..' part")
    if ext(path) not in TYPES:
        _fail(where, f"'{path}': a bundle holds only {', '.join(sorted(TYPES))} files")
    return path


def _bytes(where: str, item: dict) -> bytes | None:
    given = [k for k in ("content_base64", "text", "sha256") if item.get(k) is not None]
    if len(given) != 1:
        _fail(where, "give exactly one of content_base64, text or sha256")
    if "text" in given:
        if not isinstance(item["text"], str):
            _fail(f"{where}.text", "must be a string")
        return item["text"].encode()
    if "content_base64" in given:
        try:
            return base64.b64decode(str(item["content_base64"]), validate=True)
        except (binascii.Error, ValueError):
            _fail(f"{where}.content_base64", "is not base64")
    return None


def _store(data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    if not AppBundleBlob.objects.filter(sha256=sha).exists():
        AppBundleBlob.objects.get_or_create(sha256=sha, defaults={"size": len(data), "data": data})
    return sha


def _items(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        _fail("files", "must be a non-empty list of {path, content_base64 | text | sha256}")
    if len(raw) > MAX_FILES:
        _fail("files", f"at most {MAX_FILES}")
    if not all(isinstance(i, dict) for i in raw):
        _fail("files", "every item must be an object")
    return raw


@transaction.atomic
def store_blobs(raw) -> list[dict]:
    """Bytes for a bundle to name later by hash: [{path, sha256, size}]. Grants nothing; a blob no
    bundle names is swept."""
    out = []
    for i, item in enumerate(_items(raw)):
        where = f"files[{i}]"
        path = check_path(f"{where}.path", item.get("path"))
        data = _bytes(where, item)
        if data is None:
            _fail(where, "a blob upload carries content_base64 or text")
        if len(data) > MAX_FILE_BYTES:
            _fail(where, f"'{path}' is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        out.append({"path": path, "sha256": _store(data), "size": len(data)})
    return out


def digest_of(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(sorted(files.items()), separators=(",", ":")).encode()).hexdigest()


@transaction.atomic
def build(raw) -> dict:
    """Store a file list and return the manifest's `bundle`: {id, size, files}."""
    files: dict[str, str] = {}
    folded: set[str] = set()
    total = 0
    for i, item in enumerate(_items(raw)):
        where = f"files[{i}]"
        path = check_path(f"{where}.path", item.get("path"))
        # Two paths differing only in case are one file on most disks a bundle was built on.
        if path.lower() in folded:
            _fail(f"{where}.path", f"'{path}' is given twice")
        folded.add(path.lower())
        data = _bytes(where, item)
        if data is None:
            sha = str(item["sha256"])
            blob = AppBundleBlob.objects.filter(sha256=sha).values_list("size", flat=True).first() if _SHA.match(sha) else None
            if blob is None:
                _fail(f"{where}.sha256", "no uploaded file has that hash (send it to bundle-blobs first)")
            size = blob
        else:
            sha, size = _store(data), len(data)
        if size > MAX_FILE_BYTES:
            _fail(where, f"'{path}' is over {MAX_FILE_BYTES // (1024 * 1024)} MB")
        total += size
        files[path] = sha
    if total > MAX_BUNDLE_BYTES:
        _fail("files", f"the bundle is {total} bytes; at most {MAX_BUNDLE_BYTES} ({MAX_BUNDLE_BYTES // (1024 * 1024)} MB)")
    digest = digest_of(files)
    AppBundle.objects.get_or_create(digest=digest, defaults={"files": files, "size": total})
    return {"id": digest, "size": total, "files": sorted(files)}


def attach(raw_manifest: dict, files) -> dict:
    """A raw manifest ready for `manifest.validate`: its `bundle` is the one the server stores from
    `files`, never one the author wrote. Every route that releases a template version calls this."""
    out = dict(raw_manifest)
    out.pop("bundle", None)
    if files:
        out["bundle"] = build(files)
    return out


def validate_index(raw) -> dict | None:
    """The manifest's `bundle` as `build` wrote it (re-validated on every compose)."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not _SHA.match(str(raw.get("id") or "")):
        _fail("bundle", "is written by canopy from the uploaded files (send `files`), not by hand")
    size, files = raw.get("size"), raw.get("files")
    if not isinstance(size, int) or not 0 <= size <= MAX_BUNDLE_BYTES:
        _fail("bundle.size", "is not a bundle canopy stored")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        _fail("bundle.files", "is not a bundle canopy stored")
    return {"id": raw["id"], "size": size, "files": [check_path("bundle.files", f) for f in files]}


def url(bundle_id: str, path: str) -> str:
    """Where a bundle file is served, without the script prefix (the SPA adds its base)."""
    return f"{PREFIX}{bundle_id}/{path}"


def sweep_unused_blobs() -> int:
    """Delete uploaded blobs no bundle names, once they are old enough to be abandoned."""
    named = {sha for files in AppBundle.objects.values_list("files", flat=True) for sha in (files or {}).values()}
    old = AppBundleBlob.objects.filter(created_at__lt=timezone.now() - timedelta(days=UNUSED_BLOB_DAYS))
    stale = [pk for pk, sha in old.values_list("pk", "sha256") if sha not in named]
    AppBundleBlob.objects.filter(pk__in=stale).delete()
    return len(stale)


# --- serving -------------------------------------------------------------------------------------


def _client_version() -> str:
    return hashlib.sha256(CLIENT_FILE.read_bytes()).hexdigest()[:16]


def csp(request: HttpRequest, bundle_id: str, nonce: str | None = None) -> str:
    """The policy every file of `bundle_id` is served with (module docstring)."""
    prefix = get_script_prefix().rstrip("/")
    origins = host_origins(request)
    own = " ".join(f"{o}{prefix}{PREFIX}{bundle_id}/" for o in origins)
    client = " ".join(f"{o}{prefix}{CLIENT_PATH}" for o in origins)
    framers = " ".join(dict.fromkeys([*origins, *extension_origins()]))
    inline = f"'nonce-{nonce}' " if nonce else ""
    return "; ".join([
        "sandbox allow-scripts allow-forms",
        "default-src 'none'",
        f"script-src {inline}{client} {own}",
        f"style-src {own} 'unsafe-inline'",
        f"font-src {own} data:",
        f"img-src {own} data: blob:",
        f"media-src {own} data: blob:",
        f"connect-src {own}",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        f"frame-ancestors {framers}",
    ])


def _inject(html: bytes, nonce: str, origins: list[str], client_src: str) -> bytes:
    """canopy's client, first thing in the document, so it runs before the page's own scripts."""
    config = json.dumps(origins).replace("<", "\\u003c")
    tag = (f'<script nonce="{nonce}">window.CANOPY_DV = {{ origins: {config} }};</script>'
           f'<script nonce="{nonce}" src="{client_src}"></script>').encode()
    for pattern in (_HEAD, _HTML_TAG, _DOCTYPE):
        m = pattern.search(html)
        if m:
            return html[:m.end()] + tag + html[m.end():]
    return tag + html


def _not_found() -> HttpResponse:
    response = HttpResponse("Not found", status=404, content_type="text/plain")
    response["Cache-Control"] = "no-store"
    return response


def _common(response: HttpResponse) -> HttpResponse:
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    # The sandboxed document that loads these is an opaque origin: cross-origin to canopy.
    response["Cross-Origin-Resource-Policy"] = "cross-origin"
    response["Access-Control-Allow-Origin"] = "*"
    return response


def serve(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    rest = request.path_info[len(PREFIX):]
    if rest == "client.js":
        response = HttpResponse(CLIENT_FILE.read_bytes(), content_type=TYPES["js"])
        # Versioned by `?v=` (its hash), so a page always asks for the client it was served with.
        response["Cache-Control"] = "public, max-age=31536000, immutable" if request.GET.get("v") else "no-cache"
        return _common(response)
    bundle_id, _, path = rest.partition("/")
    if not _SHA.match(bundle_id):
        return _not_found()
    try:
        check_path("path", path)
    except ValueError:
        return _not_found()
    files = AppBundle.objects.filter(digest=bundle_id).values_list("files", flat=True).first()
    sha = (files or {}).get(path)
    data = AppBundleBlob.objects.filter(sha256=sha).values_list("data", flat=True).first() if sha else None
    if data is None:
        return _not_found()
    data = bytes(data)
    if ext(path) in HTML:
        nonce = secrets.token_urlsafe(16)
        prefix = get_script_prefix().rstrip("/")
        data = _inject(data, nonce, host_origins(request), f"{prefix}{CLIENT_PATH}?v={_client_version()}")
        response = HttpResponse(data, content_type=TYPES[ext(path)])
        response["Content-Security-Policy"] = csp(request, bundle_id, nonce)
        # A fresh nonce and the request's own origins each time.
        response["Cache-Control"] = "no-store"
    else:
        response = HttpResponse(data, content_type=TYPES[ext(path)])
        response["Content-Security-Policy"] = csp(request, bundle_id)
        response["Cache-Control"] = "public, max-age=31536000, immutable"
        response["ETag"] = f'"{sha}"'
    return _common(response)


class BundleMiddleware:
    """Answers `/app-bundles/…` before sessions, auth and CSRF exist (module docstring)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        path = request.path_info
        if not path.startswith(PREFIX) and path != PREFIX.rstrip("/"):
            return self.get_response(request)
        return serve(request)
