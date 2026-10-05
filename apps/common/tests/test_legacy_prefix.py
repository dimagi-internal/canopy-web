"""The old address after the move to canopy.dimagi.com's root.

A request that arrived as labs.connect.dimagi.com/canopy/… reaches Django with
the prefix stripped and the scope marked (config/asgi_prefix.py). Browsers are
sent on to the new address; machines keep being served where they are, with the
prefix restored for any URL generated in reply.
"""
import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import get_script_prefix, set_script_prefix

from apps.common.legacy_prefix import LegacyPrefixMiddleware
from config.asgi_prefix import SCOPE_KEY

NEW = "https://canopy.dimagi.com"


def _seen():
    seen = {}

    def view(request):
        seen["script_name"] = request.META.get("SCRIPT_NAME")
        seen["prefix"] = get_script_prefix()
        return HttpResponse("ok")

    return seen, LegacyPrefixMiddleware(view)


def _request(method, path, legacy=True):
    request = getattr(RequestFactory(), method.lower())(path)
    request.scope = {SCOPE_KEY: "/canopy"} if legacy else {}
    return request


@pytest.fixture(autouse=True)
def _reset_prefix():
    yield
    set_script_prefix("/")


@override_settings(FORCE_SCRIPT_NAME=None, CANOPY_PUBLIC_BASE_URL=NEW)
@pytest.mark.parametrize("path", ["/", "/w/connect/agents/hal?tab=work", "/walkthrough/abc/content?t=x"])
def test_a_browser_page_on_the_old_address_goes_to_the_new_one(path):
    _, mw = _seen()
    resp = mw(_request("GET", path))
    assert resp.status_code == 302
    assert resp["Location"] == NEW + path


@override_settings(FORCE_SCRIPT_NAME=None, CANOPY_PUBLIC_BASE_URL=NEW)
@pytest.mark.parametrize("path", ["/api/me", "/oauth/client.json", "/.well-known/oauth-authorization-server", "/health/"])
def test_machine_paths_are_served_in_place_with_the_prefix_restored(path):
    seen, mw = _seen()
    resp = mw(_request("GET", path))
    assert resp.status_code == 200
    assert seen == {"script_name": "/canopy", "prefix": "/canopy/"}


@override_settings(FORCE_SCRIPT_NAME=None, CANOPY_PUBLIC_BASE_URL=NEW)
def test_a_post_is_never_redirected():
    # A 302 turns a POST into a GET, losing the body: serve it where it is.
    seen, mw = _seen()
    assert mw(_request("POST", "/auth/cli/authorize/")).status_code == 200
    assert seen["script_name"] == "/canopy"


@override_settings(FORCE_SCRIPT_NAME=None, CANOPY_PUBLIC_BASE_URL=NEW)
def test_the_new_address_is_untouched():
    seen, mw = _seen()
    assert mw(_request("GET", "/w/connect", legacy=False)).status_code == 200
    assert seen["script_name"] in ("", None)


@override_settings(FORCE_SCRIPT_NAME="/canopy", CANOPY_PUBLIC_BASE_URL=NEW)
def test_inert_while_still_served_under_the_prefix():
    # Redirecting to a base that is itself prefix-served would loop.
    _, mw = _seen()
    assert mw(_request("GET", "/w/connect")).status_code == 200
