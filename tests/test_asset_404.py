"""A missing hashed bundle must 404, not be answered with the SPA shell.

Reported and reproduced 2026-09-08. Asset filenames are content-hashed and every
deploy rehashes them, so a browser holding a cached index.html — the service
worker precaches it — asks for a bundle the server no longer has. WhiteNoise
passes the miss through, and the SPA catch-all used to answer that `.js` request
with index.html at `200 text/html`.

The browser cannot parse HTML as a module, so the app rendered a WHITE PAGE until
a force-refresh, with no error anyone could act on. Six deploys in a day is what
made it routine.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("path", [
    "/assets/index-DEADBEEF.js",
    "/assets/ui-GONE.css",
    "/assets/nested/thing-OLD.js",
])
def test_a_missing_bundle_404s(client, settings, path):
    settings.REQUIRE_AUTH = False
    r = client.get(path)
    assert r.status_code == 404, (
        f"{path} returned {r.status_code} — a JS/CSS request answered with the "
        "SPA shell is what produced the white page"
    )
    assert "text/html" not in (r.headers.get("Content-Type") or "").lower() or r.status_code == 404


def test_the_spa_still_serves_app_routes(client, settings):
    """The catch-all must keep doing its job — deep links into the SPA are the
    reason it exists."""
    settings.REQUIRE_AUTH = False
    r = client.get("/supervisor")
    assert r.status_code in (200, 503)  # 503 only when no build output is present


def test_an_unauthenticated_asset_miss_does_not_redirect_to_login(client, settings):
    """The other half of the same bug: before, a miss fell through to the SPA
    route and the auth middleware bounced it to Google — so a script tag was
    answered with a sign-in page. A 404 has nothing to redirect."""
    settings.REQUIRE_AUTH = True
    r = client.get("/assets/index-DEADBEEF.js")
    assert r.status_code == 404
