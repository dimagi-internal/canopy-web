"""A connected site's page may call canopy's API cross-origin, and nothing else may.

ace-web's chat panel calls canopy from the browser with a token it minted. That
was same-origin while canopy lived at labs.connect.dimagi.com/canopy; at
canopy.dimagi.com the browser needs canopy's CORS answer (apps/tokens/cors.py).
"""
import pytest
from django.utils import timezone

from apps.tokens.models import AppCredential

SITE = "https://site.test"
PROD = {"CORS_ALLOW_ALL_ORIGINS": False, "CORS_URLS_REGEX": r"^/api/.*$"}


@pytest.fixture
def site(default_workspace):
    app = AppCredential.objects.create(name="site", workspace=default_workspace)
    app.allowed_frame_origins = [SITE]
    app.save()
    return app


def _preflight(client, origin, path="/api/me/"):
    return client.options(path, HTTP_ORIGIN=origin, HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
                          HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization")


@pytest.mark.django_db
def test_a_connected_sites_origin_may_call_the_api_without_cookies(client, site, settings):
    for k, v in PROD.items():
        setattr(settings, k, v)
    resp = _preflight(client, SITE)
    assert resp["Access-Control-Allow-Origin"] == SITE
    assert "authorization" in resp["Access-Control-Allow-Headers"].lower()
    # Never with credentials: the call carries the Bearer token and nothing else.
    assert "Access-Control-Allow-Credentials" not in resp


@pytest.mark.django_db
def test_any_other_origin_gets_no_cors_answer(client, site, settings):
    for k, v in PROD.items():
        setattr(settings, k, v)
    assert "Access-Control-Allow-Origin" not in _preflight(client, "https://elsewhere.test")


@pytest.mark.django_db
def test_a_revoked_site_loses_it(client, site, settings):
    for k, v in PROD.items():
        setattr(settings, k, v)
    site.revoked_at = timezone.now()
    site.save()
    assert "Access-Control-Allow-Origin" not in _preflight(client, SITE)


@pytest.mark.django_db
def test_only_the_api(client, site, settings):
    for k, v in PROD.items():
        setattr(settings, k, v)
    assert "Access-Control-Allow-Origin" not in _preflight(client, SITE, path="/admin/")
