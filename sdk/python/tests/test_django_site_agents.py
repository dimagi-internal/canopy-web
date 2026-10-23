"""The Django host's half of the site agents API: ``site_agents.current()`` (cached, stale
for a day, then None) and ``canopy/site.json``."""
from __future__ import annotations

from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client, override_settings

from canopy_sdk import site_agents as wire
from canopy_sdk import site as site_mod
from canopy_sdk.django import site_agents
from canopy_sdk.fetch import FetchError
from canopy_sdk.keys import private_pem

from .helpers import CANOPY, CLIENT_ID, World
from .test_site_agents import CONTENT

pytestmark = pytest.mark.django_db

T0 = 1_800_000_000.0


@pytest.fixture
def world():
    return World()


def _settings(world, **extra):
    return {"SIGNING_KEY": private_pem(world.host_key), "CANOPY_BASE_URL": CANOPY,
            "APP_NAME": "connect-labs", "AGENT_SLUG": "labsdata", "CLIENT_ID": CLIENT_ID, **extra}


@pytest.fixture
def host(world):
    cache.clear()
    with override_settings(CANOPY_HOST=_settings(world)), \
            mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        yield world
    cache.clear()


class Canopy:
    """Stands in for canopy's site agents endpoint: verifies the signed request, then
    answers with a fresh signed response, or fails."""

    def __init__(self, world):
        self.world = world
        self.calls = 0
        self.down = False
        self.content = CONTENT

    def __call__(self, url, *, headers, accept):
        self.calls += 1
        if self.down:
            raise FetchError(f"{url} could not be fetched (ConnectionRefusedError)")
        bearer = headers["Authorization"].removeprefix("Bearer ")
        claims = wire.verify_request(bearer, [self.world.host_key.public_key()],
                                          audiences=[wire.endpoint_url(CANOPY)])
        assert claims["canopy_agent"] == "labsdata"
        return wire.sign_site_agents(self.world.client.client_key, CLIENT_ID, "connect-labs", self.content)


def test_current_fetches_once_then_serves_the_cache(host):
    canopy = Canopy(host)
    first = site_agents.current(now=T0, get=canopy)
    again = site_agents.current(now=T0 + 100, get=canopy)
    assert canopy.calls == 1
    assert first.agent("labsdata").ready and not first.stale
    assert again.version == first.version and again.age == pytest.approx(100)


def test_a_failed_refresh_serves_the_last_good_response_marked_stale(host):
    canopy = Canopy(host)
    good = site_agents.current(now=T0, get=canopy)
    cache.delete(site_agents._keys(site_agents.conf.get_host_config())[0])   # the fresh copy expires
    canopy.down = True
    stale = site_agents.current(now=T0 + 3600, get=canopy)
    assert stale.stale and stale.version == good.version
    assert stale.age == pytest.approx(3600)
    # Not retried within RETRY_SECONDS: an outage is not a request per page view.
    site_agents.current(now=T0 + 3610, get=canopy)
    assert canopy.calls == 2


def test_after_a_day_of_failures_it_shows_nothing(host):
    canopy = Canopy(host)
    site_agents.current(now=T0, get=canopy)
    fresh, _, failed = site_agents._keys(site_agents.conf.get_host_config())
    cache.delete(fresh)
    canopy.down = True
    assert site_agents.current(now=T0 + site_agents.STALE_SECONDS + 1, get=canopy) is None


def test_nothing_ever_fetched_is_none(host):
    canopy = Canopy(host)
    canopy.down = True
    assert site_agents.current(now=T0, get=canopy) is None


def test_a_forged_response_is_never_cached(host):
    from canopy_sdk.keys import generate_private_key

    def forged(url, **kw):
        return wire.sign_site_agents(generate_private_key("EdDSA"), CLIENT_ID, "connect-labs", CONTENT)

    assert site_agents.current(now=T0, get=forged) is None


def test_unconfigured_host_shows_nothing():
    cache.clear()
    with override_settings(CANOPY_HOST={}):
        assert site_agents.current(get=lambda *a, **k: pytest.fail("must not fetch")) is None


def test_site_json_is_signed_and_carries_the_needs(world):
    def needs():
        return {"sensitive_data": {"requires": {"handles_sensitive": True, "runner_flag": "sensitive_data"}},
                "uses": {"agents": ["labsdata"], "sensitive_data_agents": ["labsdata"], "version": "v1"},
                "titles": {"labs:workflow:run:data": "A workflow run, read-only"}}

    cfg = _settings(world, PAGE_SCOPES={"labs:workflow:run:data": ["workflow:read", "data:read"]},
                    SITE_NEEDS=needs)
    with override_settings(CANOPY_HOST=cfg):
        response = Client().get("/canopy/site.json")
    assert response.status_code == 200 and response["Content-Type"] == "application/jose"
    got = site_mod.verify_site_document(response.content.decode(), [world.host_key.public_key()],
                                        site_name="connect-labs")
    assert got.pages[0].title == "A workflow run, read-only"
    assert got.pages[0].scopes == ("workflow:read", "data:read")
    assert got.sensitive_data_requires == {"handles_sensitive": True, "runner_flag": "sensitive_data"}
    assert got.uses_version == "v1"


def test_site_json_without_a_key_is_503():
    with override_settings(CANOPY_HOST={}):
        assert Client().get("/canopy/site.json").status_code == 503


def test_site_json_with_bad_needs_is_503(world):
    cfg = _settings(world, SITE_NEEDS=lambda: {"sensitive_data": {"requires": {"runner_flag": "nope"}}})
    with override_settings(CANOPY_HOST=cfg):
        assert Client().get("/canopy/site.json").status_code == 503


# --- naming the tenant, saying why, forcing a re-read (0.16) ---------------------------


class Asked(Canopy):
    """Records what each request named, and can refuse the way canopy does."""

    def __init__(self, world):
        super().__init__(world)
        self.named = []
        self.refusal = None

    def __call__(self, url, *, headers, accept):
        self.calls += 1
        bearer = headers["Authorization"].removeprefix("Bearer ")
        claims = wire.verify_request(bearer, [self.world.host_key.public_key()],
                                     audiences=[wire.endpoint_url(CANOPY)])
        self.named.append((claims.get("canopy_workspace"), claims.get("canopy_agent")))
        if self.refusal:
            raise FetchError(f"{url} answered 401: {self.refusal}", status=401, detail=self.refusal)
        return wire.sign_site_agents(self.world.client.client_key, CLIENT_ID, "connect-labs", self.content)


def test_workspace_names_the_tenant_and_the_agent_is_not_sent(world):
    cache.clear()
    with override_settings(CANOPY_HOST=_settings(world, WORKSPACE="connect")), \
            mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        canopy = Asked(world)
        assert site_agents.current(now=T0, get=canopy) is not None
    assert canopy.named == [("connect", None)]


def test_without_workspace_the_agent_still_names_the_tenant(host):
    canopy = Asked(host)
    site_agents.current(now=T0, get=canopy)
    assert canopy.named == [(None, "labsdata")]


def test_changing_the_tenant_selector_takes_effect_at_once(world):
    cache.clear()
    canopy = Asked(world)
    with mock.patch("canopy_sdk.host.client_keys.fetch.get_json", side_effect=world._fetch):
        with override_settings(CANOPY_HOST=_settings(world)):
            site_agents.current(now=T0, get=canopy)
        with override_settings(CANOPY_HOST=_settings(world, AGENT_SLUG="ace")):
            site_agents.current(now=T0 + 1, get=canopy)
        with override_settings(CANOPY_HOST=_settings(world, WORKSPACE="connect")):
            site_agents.current(now=T0 + 2, get=canopy)
    assert canopy.named == [(None, "labsdata"), (None, "ace"), ("connect", None)], \
        "each selector is its own cache entry, never another's copy"


def test_last_error_says_why_and_a_success_clears_it(host):
    canopy = Asked(host)
    canopy.refusal = "unknown_issuer: no connected site named 'connect-labs'"
    assert site_agents.current(now=T0, get=canopy) is None
    error = site_agents.last_error()
    assert error.code == "unknown_issuer" and error.status == 401
    assert "no connected site" in error.message
    canopy.refusal = None
    assert site_agents.refresh(now=T0 + 1, get=canopy).agent("labsdata")
    assert site_agents.last_error() is None


def test_unreachable_is_only_what_could_not_be_reached(host):
    def down(url, **kw):
        raise FetchError(f"{url} could not be fetched (ConnectionRefusedError)")

    assert site_agents.current(now=T0, get=down) is None
    assert site_agents.last_error().code == "unreachable"


def test_refresh_forces_a_re_read_past_the_cache_and_the_retry_pause(host):
    canopy = Asked(host)
    site_agents.current(now=T0, get=canopy)
    site_agents.refresh(now=T0 + 1, get=canopy)
    assert canopy.calls == 2
    canopy.refusal = "ambiguous_issuer: name the workspace"
    with pytest.raises(wire.SiteAgentsError) as exc:
        site_agents.refresh(now=T0 + 2, get=canopy)
    assert exc.value.code == "ambiguous_issuer" == site_agents.last_error().code


def test_an_unconfigured_host_says_not_configured():
    cache.clear()
    with override_settings(CANOPY_HOST={}):
        assert site_agents.last_error().code == "not_configured"
        with pytest.raises(wire.SiteAgentsError) as exc:
            site_agents.refresh(get=lambda *a, **k: pytest.fail("must not fetch"))
        assert exc.value.code == "not_configured"


def _site_json(cfg, **extra):
    with override_settings(CANOPY_HOST=cfg, **extra):
        response = Client().get("/canopy/site.json", HTTP_HOST="labs.example.org")
    assert response.status_code == 200, response.content
    return response.content.decode()


def test_site_json_declares_the_sites_facts_from_its_settings(world):
    from .helpers import ISSUER, RESOURCE, TOKEN_ENDPOINT

    cfg = _settings(world, ISSUER=ISSUER, RESOURCE=RESOURCE, TOKEN_ENDPOINT=TOKEN_ENDPOINT)
    got = site_mod.verify_site_document(_site_json(cfg), [world.host_key.public_key()],
                                        site_name="connect-labs")
    origin = ISSUER.rstrip("/")
    assert got.site.origins == (origin,)
    assert got.site.jwks_url == f"{origin}/canopy/jwks.json"
    assert got.site.issuer == ISSUER.rstrip("/") and got.site.mcp_resource == RESOURCE


def test_site_json_without_the_grant_falls_back_to_the_requests_origin(world):
    got = site_mod.verify_site_document(_site_json(_settings(world), ALLOWED_HOSTS=["*"]),
                                        [world.host_key.public_key()], site_name="connect-labs")
    assert got.site.origins == ("http://labs.example.org",)
    assert got.site.jwks_url == "http://labs.example.org/canopy/jwks.json"
    assert got.site.issuer == "" and got.site.mcp_resource == ""


def test_site_json_honours_explicit_origins_and_jwks_url(world):
    cfg = _settings(world, ORIGINS=["https://a.example.org", "https://b.example.org"],
                    JWKS_URL="https://keys.example.org/jwks.json")
    got = site_mod.verify_site_document(_site_json(cfg, ALLOWED_HOSTS=["*"]),
                                        [world.host_key.public_key()], site_name="connect-labs")
    assert got.site.origins == ("https://a.example.org", "https://b.example.org")
    assert got.site.jwks_url == "https://keys.example.org/jwks.json"
