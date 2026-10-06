"""The public product site (``site/``, built by Astro) — which paths it owns.

canopy.dimagi.com answers two audiences at one address: a signed-out visitor gets
the product site, a signed-in one gets the app. Only ``/`` depends on who is
asking; every other site page is served to everyone, and only a path that EXISTS
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
    """The site page for this request, or None to let the app answer.

    ``/`` is the site only for a visitor who is not signed in; a signed-in person
    lands on their workbench, as before.
    """
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.path
    if (path.rstrip("/") or "/") == "/" and request.user.is_authenticated:
        return None
    index = page_file(path)
    if index is None:
        return None
    response = FileResponse(open(index, "rb"), content_type="text/html; charset=utf-8")
    response["Cache-Control"] = REVALIDATE
    return response
