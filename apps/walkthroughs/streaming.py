"""Streaming endpoint for the public walkthrough viewer.

Preserved as a bare Django view (NOT ported to Ninja) — HTTP Range support
(for ``<video>`` scrubbing) doesn't fit cleanly into Ninja's contract.

Mounted at /walkthrough/<uuid:wid>/content in config/urls.py (/w/ is now the
workspace tenant prefix).
"""
from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.views.decorators.clickjacking import xframe_options_sameorigin

from . import storage
from .drive_client import DriveNotConfigured
from .models import Walkthrough

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)$")


def _parse_range(header: str, total: int) -> tuple[int, int] | None:
    """Parse a single-range HTTP Range header. Multi-range not supported."""
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    start = int(m.group(1))
    end_raw = m.group(2)
    end = int(end_raw) if end_raw else total - 1
    if start > end or start >= total:
        return None
    return start, min(end, total - 1)


# Token-gated, so `private` (never a shared cache) — but the bytes behind one
# walkthrough id are immutable, so the BROWSER should keep them. Without this
# every scrub back to a spot already watched re-fetched it from Drive at ~1.3s.
_CACHE_CONTROL = "private, max-age=86400, immutable"


def _etag(w) -> str:
    """Stable per-file validator, so a revalidation is a 304 not a re-download."""
    return f'"{w.drive_file_id}-{w.size_bytes}"'


def _get_or_404(wid):
    # ValidationError: the route now accepts <str:wid>, so a non-UUID id reaches
    # the UUIDField pk lookup and raises ValidationError (not ValueError) — treat
    # it as "not found" so a malformed content URL 404s instead of 500ing.
    try:
        return Walkthrough.objects.get(pk=wid)
    except (Walkthrough.DoesNotExist, ValueError, ValidationError):
        return None


@xframe_options_sameorigin
def walkthrough_content(request, wid):
    """GET /w/<id>/content — stream the file bytes from Drive.

    Auth: any authenticated session user OR a public (visibility=link)
    walkthrough presented with its ?t=<share_token>. Anything else 404s
    so existence isn't leaked.

    Django's SecurityMiddleware sets ``X-Frame-Options: DENY`` globally,
    which breaks our own viewer page (``/w/<id>``) when it tries to embed
    this endpoint via ``<iframe src=...>``. Override to ``SAMEORIGIN`` —
    the viewer is the only intended embedder and lives on the same host.
    """
    if not getattr(settings, "WALKTHROUGHS_ENABLED", True):
        raise Http404("walkthroughs disabled")

    w = _get_or_404(wid)
    if w is None:
        raise Http404("walkthrough not found")

    # Readable by a MEMBER of the walkthrough's workspace, or by anyone with a
    # matching ?t=<share_token> (visibility=link). A non-member (even authenticated)
    # 404s exactly like private, so existence never leaks — and can't stream another
    # workspace's file bytes.
    if not w.readable_by(request):
        raise Http404("walkthrough not found")

    range_hdr = request.META.get("HTTP_RANGE", "")
    try:
        if range_hdr:
            # The total is only needed to clamp the range, and we already know
            # it: `size_bytes` is recorded at upload and the bytes never change
            # (there is no in-place replace — a new cut is a new walkthrough).
            #
            # This used to be a `download(start=0, end=0)` head request, which
            # meant EVERY range the <video> element asked for cost two
            # sequential Drive round-trips instead of one. Measured against
            # prod: ~1.93s TTFB with the probe, ~1.37s without — ~0.55s of pure
            # latency added to the initial load and to every single seek. A
            # scrub is several ranges, so it compounded.
            #
            # Falls back to the probe when size_bytes is missing (rows that
            # predate the field), so nothing that worked stops working.
            total = w.size_bytes
            if not total:
                _, _, _, total = storage.download(
                    file_id=w.drive_file_id, start=0, end=0,
                )
            parsed = _parse_range(range_hdr, total)
            if parsed is None:
                resp = HttpResponse(status=416)
                resp["Content-Range"] = f"bytes */{total}"
                return resp
            start, end = parsed
            data, s, e, t = storage.download(
                file_id=w.drive_file_id, start=start, end=end,
            )
            resp = StreamingHttpResponse(
                iter([data]),
                status=206,
                content_type=w.content_type,
            )
            # `t` comes back from the byte fetch itself, so the denominator we
            # publish is Drive's own count even if size_bytes ever drifted.
            resp["Content-Range"] = f"bytes {s}-{e}/{t}"
            resp["Content-Length"] = str(len(data))
            resp["Accept-Ranges"] = "bytes"
            resp["Cache-Control"] = _CACHE_CONTROL
            resp["ETag"] = _etag(w)
            return resp

        data, s, e, t = storage.download(file_id=w.drive_file_id)
        resp = StreamingHttpResponse(
            iter([data]),
            status=200,
            content_type=w.content_type,
        )
        resp["Content-Length"] = str(len(data))
        resp["Accept-Ranges"] = "bytes"
        resp["Cache-Control"] = _CACHE_CONTROL
        resp["ETag"] = _etag(w)
        return resp
    except DriveNotConfigured:
        return HttpResponse(status=500)
    except Exception:
        return HttpResponse(status=502)
