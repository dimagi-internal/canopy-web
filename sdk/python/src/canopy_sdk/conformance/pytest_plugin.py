"""pytest fixtures for a host's own CI. Opt in (it is not auto-loaded)::

    # conftest.py
    pytest_plugins = ["canopy_sdk.conformance.pytest_plugin"]

In-process fixtures (no network):

* ``canopy_client`` — a ``ClientCredentials`` standing in for canopy, with
  fresh keys and client_id ``https://canopy.test/oauth/client.json``.
* ``canopy_client_documents`` — ``{url: document}`` for that client's CIMD and
  JWKS: stub your ``ClientKeyResolver``'s fetch with ``documents.__getitem__``.
* ``canopy_redeem`` — ``canopy_redeem(id_jag, token_endpoint, resource,
  issuer)`` → ``(form, dpop_header)`` exactly as canopy would POST them.
* ``canopy_mcp_headers`` — ``canopy_mcp_headers(access_token, url, method)`` →
  the ``Authorization`` + ``DPoP`` headers canopy sends to your MCP.

Live checks (skipped unless asked for)::

    pytest --canopy-issuer https://labs.example.org \\
           --canopy-resource https://labs.example.org/mcp/ \\
           --canopy-jwks https://labs.example.org/labs/canopy/jwks.json

* ``canopy_live`` — ``{"issuer", "resource", "jwks"}`` from those options; the
  test is skipped when they are absent, so network use stays explicit.
"""
from __future__ import annotations

import pytest

from .. import consumer, contract

CLIENT_ID = "https://canopy.test/oauth/client.json"
JWKS_URI = "https://canopy.test/oauth/jwks.json"


def pytest_addoption(parser):
    group = parser.getgroup("canopy", "canopy host conformance")
    group.addoption("--canopy-issuer", default="", help="the host's OAuth issuer (live checks)")
    group.addoption("--canopy-resource", default="", help="the host's MCP URL (live checks)")
    group.addoption("--canopy-jwks", default="", help="the host's published JWKS URL (live checks)")


@pytest.fixture
def canopy_client() -> consumer.ClientCredentials:
    return consumer.ClientCredentials.generate(CLIENT_ID)


@pytest.fixture
def canopy_client_documents(canopy_client) -> dict:
    return {
        CLIENT_ID: canopy_client.metadata(JWKS_URI),
        JWKS_URI: canopy_client.jwks(),
    }


@pytest.fixture
def canopy_redeem(canopy_client):
    def make(id_jag: str, token_endpoint: str, resource: str, issuer: str):
        form = consumer.redemption_form(id_jag, client_id=canopy_client.client_id, resource=resource)
        form["client_assertion"] = canopy_client.client_assertion(contract.normalize_url(issuer))
        return form, canopy_client.dpop_proof("POST", token_endpoint)

    return make


@pytest.fixture
def canopy_mcp_headers(canopy_client):
    def make(access_token: str, url: str, method: str = "POST") -> dict:
        return {"Authorization": consumer.dpop_authorization(access_token),
                contract.DPOP_HEADER: canopy_client.dpop_proof(method, url, access_token=access_token)}

    return make


@pytest.fixture
def canopy_live(request) -> dict:
    opts = {name: request.config.getoption(f"--canopy-{name}") for name in ("issuer", "resource", "jwks")}
    if not (opts["issuer"] and opts["resource"]):
        pytest.skip("live canopy conformance needs --canopy-issuer and --canopy-resource")
    return opts
