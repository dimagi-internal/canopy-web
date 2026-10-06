"""The public product site at `/` (config/public_site.py, config/static_middleware.py).

`/` answers two audiences: a signed-out visitor gets the site, a signed-in person
their workbench. Every other site page is served to everyone, and ONLY a path that
exists in the site build is ever the site's, so an app route cannot be shadowed by
it. These tests build a fake `site/dist` and `frontend/dist` so the routing is
checked without either real build.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from config import public_site
from config.static_middleware import CanopyWhiteNoiseMiddleware

SITE_HOME = "<html>the public site home</html>"
SITE_PAGE = "<html>how it works</html>"
SPA = "<html>the spa shell</html>"


@pytest.fixture
def dists(tmp_path, settings):
    site = tmp_path / "site-dist"
    (site / "how-it-works").mkdir(parents=True)
    (site / "site" / "assets").mkdir(parents=True)
    (site / "index.html").write_text(SITE_HOME)
    (site / "how-it-works" / "index.html").write_text(SITE_PAGE)
    (site / "site" / "assets" / "Base-AbCdEf12.css").write_text("body{}")
    spa = tmp_path / "spa-dist"
    spa.mkdir()
    (spa / "index.html").write_text(SPA)
    settings.SITE_DIST_DIR = site
    settings.FRONTEND_DIST_DIR = spa
    public_site._pages.cache_clear()
    yield site
    public_site._pages.cache_clear()


def _body(resp) -> str:
    return b"".join(resp.streaming_content).decode() if resp.streaming else resp.content.decode()


@pytest.fixture
def member(db):
    return get_user_model().objects.create_user(username="m", email="m@dimagi.com", password="x")


@override_settings(REQUIRE_AUTH=True)
def test_signed_out_root_is_the_site(dists, db):
    resp = Client().get("/")
    assert resp.status_code == 200
    assert _body(resp) == SITE_HOME
    assert resp["Cache-Control"] == "no-cache"


@override_settings(REQUIRE_AUTH=True)
def test_signed_in_root_is_the_app(dists, member):
    c = Client()
    c.force_login(member)
    assert _body(c.get("/")) == SPA


@override_settings(REQUIRE_AUTH=True)
@pytest.mark.parametrize("path", ["/how-it-works", "/how-it-works/"])
def test_site_pages_are_public(dists, db, path):
    resp = Client().get(path)
    assert resp.status_code == 200
    assert _body(resp) == SITE_PAGE


@override_settings(REQUIRE_AUTH=True)
def test_site_pages_are_served_to_members_too(dists, member):
    c = Client()
    c.force_login(member)
    assert _body(c.get("/how-it-works")) == SITE_PAGE


@override_settings(REQUIRE_AUTH=True)
def test_an_app_route_is_never_the_sites(dists, db):
    resp = Client().get("/supervisor")
    assert resp.status_code == 302
    assert "/accounts/google/login/" in resp["Location"]


@override_settings(REQUIRE_AUTH=True)
def test_without_a_site_build_root_still_asks_for_sign_in(tmp_path, settings, db):
    settings.SITE_DIST_DIR = tmp_path / "missing"
    public_site._pages.cache_clear()
    try:
        resp = Client().get("/")
        assert resp.status_code == 302
        assert "/accounts/google/login/" in resp["Location"]
    finally:
        public_site._pages.cache_clear()


def test_site_assets_are_not_pages(dists):
    assert set(public_site.pages()) == {"/", "/how-it-works"}
    assert public_site.is_public_path("/site/assets/Base-AbCdEf12.css")


def test_whitenoise_leaves_root_to_django_and_serves_site_assets(dists, settings):
    """WhiteNoise runs before auth, so it must not answer `/` with the SPA shell:
    only Django knows whether the visitor is signed in."""
    settings.WHITENOISE_ROOT = settings.FRONTEND_DIST_DIR
    settings.WHITENOISE_INDEX_FILE = True
    settings.WHITENOISE_AUTOREFRESH = False
    sentinel = object()
    mw = CanopyWhiteNoiseMiddleware(lambda request: sentinel)

    from django.test import RequestFactory
    rf = RequestFactory()
    assert mw(rf.get("/")) is sentinel
    asset = mw(rf.get("/site/assets/Base-AbCdEf12.css"))
    assert asset is not sentinel and asset.status_code == 200
    assert "immutable" in asset["Cache-Control"]
