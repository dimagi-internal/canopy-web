"""The public product site (``site/``, built by Astro) — which paths it owns.

``/`` is the product site for EVERYONE, signed in or not, and ``/app`` is the door
into the app (the SPA sends it to your default workspace; signed out, the login
middleware sends you through Google first). That is deliberate for now (owner,
2026-10-06): the site is being iterated on and has to be visible while signed in.
The earlier rule — signed-in ``/`` went straight to the workbench — is one line
in ``serve`` to restore. Only a path that EXISTS
in ``site/dist`` is ever the site's, so no app route can be shadowed by it — the
set is read from the build output rather than hand-listed.

The site's own files (CSS, scripts) are built under ``/site/…``
(``site/astro.config.mjs``) so they cannot collide with the SPA's ``/assets/``;
``config.static_middleware`` serves them.

Kept out of ``apps/`` on purpose: the site is content, not a feature, and
``site/`` is meant to lift onto its own host one day without touching Django.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse

from config.static_cache import REVALIDATE

ASSET_PREFIX = "/site/"


def dist_dir() -> Path:
    return Path(settings.SITE_DIST_DIR)


@lru_cache(maxsize=1)
def _pages(root: str) -> dict[str, Path]:
    """URL path → HTML file, for every page in the build (``a/b/index.html`` → ``/a/b``)."""
    base = Path(root)
    if not base.is_dir():
        return {}
    pages: dict[str, Path] = {}
    for index in base.rglob("index.html"):
        rel = index.parent.relative_to(base).as_posix()
        if rel == "site" or rel.startswith("site/"):
            continue  # assets, not pages
        pages["/" if rel == "." else f"/{rel}"] = index
    return pages


def pages() -> dict[str, Path]:
    return _pages(str(dist_dir()))


def page_file(path: str) -> Path | None:
    """The built page for ``path`` (trailing slash optional), or None."""
    key = path.rstrip("/") or "/"
    return pages().get(key)


def is_public_path(path: str) -> bool:
    """For the login middleware: may a signed-out visitor GET this?"""
    return page_file(path) is not None or path.startswith(ASSET_PREFIX)


def serve(request: HttpRequest) -> HttpResponse | None:
    """The site page for this request, or None to let the app answer."""
    if request.method not in ("GET", "HEAD"):
        return None
    index = page_file(request.path)
    if index is None:
        return None
    response = FileResponse(open(index, "rb"), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = REVALIDATE
    return response
