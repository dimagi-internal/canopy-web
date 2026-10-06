"""Which other websites may call canopy's API from a browser.

A connected site's pages can talk to canopy directly — ace-web's chat panel
calls canopy's REST and socket with a token it minted for the visitor. While
canopy lived on the same host (labs.connect.dimagi.com/canopy) that was a
same-origin call and needed nothing; at canopy.dimagi.com it is cross-origin,
and a browser refuses it unless canopy says the site's origin may.

The origins are the ones each live connected site already declares (Settings →
Connected sites: the websites its pages are served from), so connecting a site
is still the whole setup. Credentials are never allowed (`CORS_ALLOW_CREDENTIALS`
stays off): the browser sends no canopy cookie on such a call, so it carries
exactly the authority of the Bearer token the page holds and nothing else. That
is why this is not an access decision — anyone can make the same call with curl.
"""
from __future__ import annotations


def connected_site_origin(sender, request, **kwargs) -> bool:
    """`corsheaders.signals.check_request_enabled`: True when the request's
    Origin is declared by a live connected site."""
    # corsheaders enables CORS when the path matches CORS_URLS_REGEX OR this
    # signal says yes, so the path has to be checked here as well.
    if not request.path_info.startswith("/api/"):
        return False
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if not origin:
        return False
    from .models import AppCredential

    for origins in AppCredential.objects.filter(revoked_at__isnull=True).values_list(
            "allowed_frame_origins", flat=True):
        if origin in (origins or []):
            return True
    return False
