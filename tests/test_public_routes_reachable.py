"""Every chrome-less public surface must pass the login-middleware allowlist.

There are THREE allowlists for these routes and they can silently disagree:

  apps/common/middleware.py       whether the SERVER serves the request
  frontend/src/api/client.v2      whether a 401 bounces the browser to Google
  frontend/src/auth/AuthProvider  what gets painted

`/narrative/` shipped in the two frontend lists and NOT in the middleware, so an
anonymous reader loaded the shared arc fine and hit a Google login the moment
they clicked "Read the scenes" — the exact link a domain expert was about to be
sent. Nothing caught it: the API answered the token correctly, and the page only
rendered for me because my browser held an authenticated session.

Note this asserts against the ALLOWLIST PREDICATE, not an HTTP status. The test
settings disable REQUIRE_AUTH, so a status-code test passes for every route and
would prove nothing — the first version of this file did exactly that.

The frontend half of the guard is frontend/src/auth/publicLinkRoutes.test.ts.
"""
from __future__ import annotations

import pytest

from apps.common.middleware import (
    _is_ddd_release_link,
    _is_invite_link,
    _is_public,
    _is_review_link,
    _is_share_link,
    _is_storyboard_link,
    _is_walkthrough_link,
)


class _Req:
    """The minimum LoginRequiredMiddleware's predicates read off a request."""

    def __init__(self, path: str, method: str = "GET"):
        self.path = path
        self.method = method


def _allowlisted(path: str, method: str = "GET") -> bool:
    """Mirrors the disjunction in LoginRequiredMiddleware.__call__."""
    request = _Req(path, method)
    return (
        _is_public(path)
        or _is_walkthrough_link(request)
        or _is_review_link(path)
        or _is_share_link(path)
        or _is_ddd_release_link(request)
        or _is_storyboard_link(request)
        or _is_invite_link(request)
    )


# Every surface mounted outside the app shell. A new one belongs here the day it
# ships — being served while logged out is the entire point of a share link.
PUBLIC_PATHS = [
    "/storyboard/ecf-supply",
    "/narrative/verified-monitoring",
    "/ddd-release/verified-monitoring/verified-monitoring-2026-07-26-001",
    "/review/00000000-0000-0000-0000-000000000000/",
    "/share/any-token",
    "/invite/any-token",
    "/api/storyboards/ecf-supply",
    "/api/storyboards/ecf-supply/narratives/verified-monitoring",
    "/api/storyboards/ecf-supply/feedback",
]


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_a_public_surface_is_allowlisted(path):
    assert _allowlisted(path), (
        f"{path} is not allowlisted, so an anonymous visitor is redirected to "
        f"Google. Add it in apps/common/middleware.py."
    )


# The allowlist must stay an ALLOWLIST. Without these, a regression that
# admitted everything would make every test above pass for the wrong reason.
GATED_PATHS = [
    "/supervisor",
    "/insights",
    "/settings",
    "/sessions",
    "/w/dimagi",
    "/ddd/verified-monitoring",
    "/api/agents/",
    "/api/feedback/",
]


@pytest.mark.parametrize("path", GATED_PATHS)
def test_an_authenticated_surface_is_not_allowlisted(path):
    assert not _allowlisted(path), f"{path} is reachable without login"


def test_the_ddd_console_is_gated_even_though_ddd_release_is_public():
    """A prefix check that was too loose here would expose the operator console."""
    assert _allowlisted("/ddd-release/x/y")
    assert not _allowlisted("/ddd/x")
    assert not _allowlisted("/api/ddd/narratives/x/")
