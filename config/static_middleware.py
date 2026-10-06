"""WhiteNoise, with two changes for the public site (see config/public_site.py).

1. ``/`` is NOT answered from ``frontend/dist/index.html``. WhiteNoise runs before
   sessions and auth, so it cannot know whether the visitor is signed in, and that
   is exactly what decides whether ``/`` is the site or the app. It falls through to
   Django, where ``spa_view`` asks ``public_site.serve``.
2. The site's own files under ``site/dist/site/`` are served at ``/site/``, with the
   same cache rule as the SPA's (hashed ⇒ immutable, else revalidate).
"""
from __future__ import annotations

from django.conf import settings
from whitenoise.middleware import WhiteNoiseMiddleware


class CanopyWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    def __init__(self, get_response=None):
        super().__init__(get_response)
        site_assets = settings.SITE_DIST_DIR / "site"
        if site_assets.is_dir():
            self.add_files(str(site_assets), prefix="site/")

    def __call__(self, request):
        if request.path_info == "/":
            return self.get_response(request)
        return super().__call__(request)
