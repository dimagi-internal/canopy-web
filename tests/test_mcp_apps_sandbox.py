"""The MCP Apps sandbox proxy and the CSP a View runs under (spec 2026-10-08 §7).

Owner decision 1: no new DNS — the proxy is a canopy sub-path made OPAQUE by the
host's `sandbox="allow-scripts"` (no `allow-same-origin`) and by its own
`Content-Security-Policy: sandbox allow-scripts`. These pin what keeps that
equivalent to a separate origin: it sets no cookie, holds no session, serves
nothing else, and never widens what the site's View declared.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.tokens import mcp_apps
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

SITE = {"https://labs.test"}


def directives(header: str) -> dict:
    out = {}
    for part in header.split(";"):
        bits = part.strip().split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


# --- the CSP ---------------------------------------------------------------------------


def test_omitted_csp_is_the_spec_default_narrowed():
    d = directives(mcp_apps.csp_header({}, frame_ancestors=["https://canopy.test"]))
    assert d["default-src"] == ["'none'"]
    assert d["script-src"] == ["'unsafe-inline'"]
    assert d["style-src"] == ["'unsafe-inline'"]
    assert d["img-src"] == ["data:"] and d["media-src"] == ["data:"]
    assert d["connect-src"] == ["'none'"] and d["frame-src"] == ["'none'"]
    assert d["object-src"] == ["'none'"] and d["form-action"] == ["'none'"]
    assert d["base-uri"] == ["'none'"]
    assert d["frame-ancestors"] == ["https://canopy.test"]
    assert d["sandbox"] == ["allow-scripts"]
    assert "allow-same-origin" not in str(d)


def test_declared_domains_are_kept_only_when_the_site_registered_them():
    csp, dropped = mcp_apps.effective_csp({
        "connectDomains": ["https://labs.test", "wss://labs.test", "https://evil.test",
                           "https://*.labs.test"],
        "resourceDomains": ["https://cdn.jsdelivr.net"],
        "frameDomains": ["https://labs.test/path"],
    }, SITE)
    assert csp == {"connectDomains": ["https://labs.test", "wss://labs.test"]}
    assert any("evil.test" in d for d in dropped)
    assert any("cdn.jsdelivr" in d for d in dropped)
    assert any("*.labs.test" in d for d in dropped)


@pytest.mark.parametrize("value", [
    "https://labs.test; script-src *", "'unsafe-eval'", "*", "https://labs.test 'self'",
    "javascript:alert(1)", "http://labs.test", "https://labs.test/x?y",
])
def test_a_declared_value_cannot_smuggle_a_directive_or_keyword(value):
    assert mcp_apps.normalize_source(value) is None
    csp, _ = mcp_apps.effective_csp({"connectDomains": [value]}, SITE | {value})
    assert csp == {}


def test_the_sandbox_url_round_trips_the_effective_csp(settings):
    settings.MCP_APPS_SANDBOX_URL = "/mcp-apps/sandbox/"
    assert mcp_apps.sandbox_src({}) == "/mcp-apps/sandbox/"
    src = mcp_apps.sandbox_src({"connectDomains": ["https://labs.test"]})
    assert src.startswith("/mcp-apps/sandbox/?csp=")
    assert mcp_apps.decode_csp(src.split("csp=")[1]) == {"connectDomains": ["https://labs.test"]}
    assert mcp_apps.decode_csp("!!not-base64!!") == {}


def test_a_dedicated_sandbox_origin_is_config_only(settings):
    settings.MCP_APPS_SANDBOX_URL = "https://canopy-sandbox.example/proxy"
    assert mcp_apps.sandbox_src({}) == "https://canopy-sandbox.example/proxy"


# --- the proxy page ----------------------------------------------------------------------


@pytest.fixture()
def site():
    owner = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    app = AppCredential.create_credential(name="connect-labs", created_by=owner, workspace=ws)
    app.allowed_frame_origins = ["https://labs.connect.test"]
    app.save()
    return {"owner": owner, "app": app}


def test_the_proxy_serves_with_an_opaque_sandbox_and_no_cookies(site, settings):
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.test"
    client = Client()
    client.force_login(site["owner"])  # a signed-in viewer's cookies ride along
    r = client.get("/mcp-apps/sandbox/?csp=" + mcp_apps.encode_csp(
        {"connectDomains": ["https://labs.connect.test"]}))
    assert r.status_code == 200
    assert r["Content-Type"].startswith("text/html")
    assert not r.cookies, "the proxy must never set (or refresh) a cookie"
    assert "Set-Cookie" not in str(r.headers)
    d = directives(r["Content-Security-Policy"])
    assert d["sandbox"] == ["allow-scripts"]
    assert d["connect-src"] == ["https://labs.connect.test"]
    # canopy's own pages and every Connected site's (the embed nests four deep).
    assert {"https://canopy.test", "https://labs.connect.test"} <= set(d["frame-ancestors"])
    assert "X-Frame-Options" not in r.headers
    assert r["Referrer-Policy"] == "no-referrer"
    body = r.content.decode()
    assert 'setAttribute("sandbox", "allow-scripts")' in body
    assert "allow-same-origin" not in body.replace("never allow-same-origin", "")
    assert '"https://canopy.test"' in body  # the host origins it accepts messages from


def test_the_proxy_is_public_but_holds_no_session(site):
    r = Client().get("/mcp-apps/sandbox/")
    assert r.status_code == 200 and not r.cookies


def test_every_other_path_and_method_is_refused(site):
    c = Client()
    assert c.get("/mcp-apps/").status_code == 404
    assert c.get("/mcp-apps/sandbox/other.js").status_code == 404
    assert c.get("/mcp-apps/anything").status_code == 404
    assert c.post("/mcp-apps/sandbox/").status_code == 405


def test_a_tampered_csp_query_falls_back_to_the_restrictive_default(site):
    r = Client().get("/mcp-apps/sandbox/?csp=eyJjb25uZWN0RG9tYWlucyI6WyIqIl19")  # ["*"]
    assert directives(r["Content-Security-Policy"])["connect-src"] == ["'none'"]


def test_the_opaque_origin_gets_no_cors_from_the_api(site):
    r = Client().options("/api/canopy-sessions/", HTTP_ORIGIN="null",
                         HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
    assert "Access-Control-Allow-Origin" not in r.headers
    assert "Access-Control-Allow-Credentials" not in r.headers
