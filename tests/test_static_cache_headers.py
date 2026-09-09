"""The SPA shell must never be served stale; hashed assets must never expire.

Both halves are load-bearing for "do I have to hard-refresh to see a deploy?".
A stale `index.html` names the previous build's asset hashes, so the browser
renders the previous app — for however long the Cache-Control allows. WhiteNoise
defaulted that to 60 seconds and `spa_view` sent nothing at all.
"""
import pytest
from django.conf import settings
from django.test import Client

from config.static_cache import IMMUTABLE, REVALIDATE, add_cache_headers, is_hashed_asset


class TestHashedAssetDetection:
    @pytest.mark.parametrize(
        "url",
        [
            "/assets/index-D2OGee1N.js",
            "/assets/index-e91UaNHK.css",
            # the name itself contains dashes — the hash is the LAST segment
            "/assets/geist-latin-wght-normal-Dm3htQBi.woff2",
            "/assets/workbox-window.prod.es5-Bd17z0YL.js",
            "/canopy/assets/ChatPage-BnQUZ0gB.js",
        ],
    )
    def test_hashed_assets_are_immutable(self, url):
        assert is_hashed_asset(url)

    @pytest.mark.parametrize(
        "url",
        [
            "/index.html",
            "/canopy/index.html",
            "/sw.js",  # names the whole precache manifest — must be current
            "/sw-push.js",
            "/manifest.webmanifest",
            "/favicon.svg",
            "/icons/icon-192.png",
            # unhashed file that happens to live under assets/
            "/assets/logo.svg",
        ],
    )
    def test_everything_else_revalidates(self, url):
        assert not is_hashed_asset(url)

    def test_the_hook_sets_the_two_policies(self):
        headers = {}
        add_cache_headers(headers, "/x", "/assets/index-D2OGee1N.js")
        assert headers["Cache-Control"] == IMMUTABLE

        headers = {}
        add_cache_headers(headers, "/x", "/index.html")
        assert headers["Cache-Control"] == REVALIDATE

    def test_the_hook_overwrites_whitenoises_own_value(self):
        # WhiteNoise sets max-age=60 first; the hook runs after and must win,
        # not append.
        headers = {"Cache-Control": "max-age=60, public"}
        add_cache_headers(headers, "/x", "/index.html")
        assert headers["Cache-Control"] == REVALIDATE

    def test_it_leaves_whitenoises_own_immutable_verdict_alone(self):
        # WhiteNoise marks Django's hashed /static/ files (admin, allauth via
        # ManifestStaticFilesStorage) immutable itself, and this hook runs AFTER
        # it. Those names are not under /assets/, so overwriting them would
        # downgrade a permanent cache to a 304 on every page load.
        headers = {"Cache-Control": "max-age=315360000, public, immutable"}
        add_cache_headers(headers, "/x", "/static/admin/css/base.5f2b0f5b8e6b.css")
        assert headers["Cache-Control"] == "max-age=315360000, public, immutable"

    def test_it_is_wired_into_whitenoise(self):
        assert settings.WHITENOISE_ADD_HEADERS_FUNCTION is add_cache_headers


@pytest.mark.django_db
def test_spa_view_marks_the_shell_no_cache(tmp_path, settings):
    """A deep link (/supervisor, /share/<token>, …) is served by spa_view, not
    WhiteNoise. It shipped with no Cache-Control at all."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html></html>")
    settings.FRONTEND_DIST_DIR = dist

    response = Client().get("/share/some-token")

    assert response.status_code == 200
    assert response["Cache-Control"] == REVALIDATE
