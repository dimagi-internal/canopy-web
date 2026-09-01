"""A URL pointing back at the current request must keep the /canopy prefix.

On labs.connect.dimagi.com/canopy the ASGI wrapper strips the prefix before
Django sees the path, and Django's ASGIRequest — unlike WSGIRequest — sets
`request.path` straight from the stripped scope. So `get_full_path()` returns
a path that belongs to the SIBLING tenant (Connect Labs), not to canopy-web.

Two places hand such a URL back to a browser, and both 404'd a first-time
operator minting a CLI token on the deployed instance:
  - the CLI authorize page's form `action` (the Authorize button POSTed to
    Connect Labs),
  - the `?next=` target of the pre-login bounce (a successful sign-in landed
    on Connect Labs).

`self_full_path` is the one home for that idiom; these tests pin both the
helper and the two call sites.
"""
from __future__ import annotations

from django.test import RequestFactory

from apps.common.middleware import _is_public
from apps.common.script_prefix import self_full_path


def _req(path, script_name="", **params):
    request = RequestFactory().get(path, params)
    request.META["SCRIPT_NAME"] = script_name
    # RequestFactory builds a WSGIRequest, which derives path from
    # SCRIPT_NAME + PATH_INFO at construction time. Under ASGI the prefix is
    # absent from request.path entirely, which is the case being reproduced —
    # so pin path/path_info to the stripped shape the wrapper actually yields.
    request.path = path
    request.path_info = path
    return request


def test_self_full_path_is_a_noop_without_a_prefix():
    # local dev and GCP: FORCE_SCRIPT_NAME unset, nothing to re-add
    assert self_full_path(_req("/auth/cli/authorize/")) == "/auth/cli/authorize/"


def test_self_full_path_restores_the_script_prefix():
    got = self_full_path(_req("/auth/cli/authorize/", script_name="/canopy"))
    assert got == "/canopy/auth/cli/authorize/"


def test_self_full_path_preserves_the_query_string():
    # the CLI handshake carries ?cb/?state/?label through an OAuth round trip;
    # losing them produces a "missing state" page that looks like a server bug
    got = self_full_path(
        _req(
            "/auth/cli/authorize/",
            script_name="/canopy",
            cb="http://127.0.0.1:53123/cb",
            state="nonce123",
            label="canopy-cli",
        )
    )
    assert got.startswith("/canopy/auth/cli/authorize/?")
    assert "state=nonce123" in got
    assert "cb=http" in got
    assert "label=canopy-cli" in got


def test_authorize_page_is_not_in_the_public_allowlist():
    # It used to be, so that the view's own @login_required would bounce and
    # keep ?cb/?state/?label. That decorator builds ?next= from
    # get_full_path(), which drops the prefix — so the bounce must come from
    # LoginRequiredMiddleware, which preserves the query AND the prefix.
    assert _is_public("/auth/cli/authorize/") is False


def test_the_rest_of_the_public_allowlist_is_untouched():
    assert _is_public("/accounts/google/login/") is True
    assert _is_public("/health/") is True
    assert _is_public("/api/mcp/") is True
