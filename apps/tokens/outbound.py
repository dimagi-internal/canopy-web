"""Outbound requests to a URL an operator typed — the host grant flow's only door out.

Every fetch the host grant flow makes (RFC 8414 metadata, the token endpoint,
the host's MCP server) goes to a URL that came from a Connected site's config
or from a document that URL returned. That makes each one a request canopy
makes on an operator's word from inside a VPC with a metadata service, so the
guards `jwks.py` established apply to every one of them, and live here once:

* **https only** — a token sent in the clear is a token handed to the path.
* **no private address space**, checked on every resolved address
  (`jwks._refuse_private`), because a public name can resolve anywhere.
* **no redirects** — the cheapest way around a host check, and a way to carry
  a DPoP-bound token to a URL its proof was not made for.
* **bounded time and size**.

Residual, as in `jwks.py`: DNS rebinding between the check and the connection is
not defeated here; what comes back must still be a well-formed document for the
exact flow in progress, so a rebound host yields nothing usable.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import requests

from . import jwks as _jwks

TIMEOUT_SECONDS = 5
MAX_BYTES = 256 * 1024


class OutboundError(Exception):
    """Why canopy will not, or could not, make this request. Safe to show an operator:
    it never contains a credential."""


def refuse_private(host: str) -> None:
    try:
        _jwks._refuse_private(host)
    except _jwks.JwksError as exc:
        raise OutboundError(str(exc).replace("a JWKS URL", "this URL")) from exc


def check_url(url: str, *, what: str = "URL") -> str:
    """Validate an outbound target. Returns it unchanged, or raises."""
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise OutboundError(f"the {what} must be https: {value!r}")
    if not parsed.hostname:
        raise OutboundError(f"the {what} has no host: {value!r}")
    if parsed.username or parsed.password:
        raise OutboundError(f"the {what} must not carry credentials")
    refuse_private(parsed.hostname)
    return value


def _read(resp) -> dict:
    body = resp.raw.read(MAX_BYTES + 1, decode_content=True) or b""
    if len(body) > MAX_BYTES:
        raise OutboundError(f"the response was larger than {MAX_BYTES} bytes")
    try:
        doc = json.loads(body or b"{}")
    except ValueError as exc:
        raise OutboundError("the response was not JSON") from exc
    if not isinstance(doc, dict):
        raise OutboundError("the response was not a JSON object")
    return doc


def get_json(url: str, *, what: str = "URL") -> dict:
    check_url(url, what=what)
    try:
        resp = requests.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False,
                            headers={"Accept": "application/json"}, stream=True)
    except requests.RequestException as exc:
        raise OutboundError(f"could not reach the {what} ({type(exc).__name__})") from exc
    if resp.status_code != 200:
        raise OutboundError(f"the {what} answered {resp.status_code}")
    return _read(resp)


def post_form(url: str, data: dict, *, headers: dict, what: str = "URL"):
    """POST a form. Returns `(status, json_body, response_headers)` — the caller
    reads OAuth errors from the body, so a non-200 is not raised here."""
    check_url(url, what=what)
    try:
        resp = requests.post(url, data=data, timeout=TIMEOUT_SECONDS, allow_redirects=False,
                             headers={"Accept": "application/json", **headers}, stream=True)
    except requests.RequestException as exc:
        raise OutboundError(f"could not reach the {what} ({type(exc).__name__})") from exc
    if 300 <= resp.status_code < 400:
        raise OutboundError(f"the {what} redirected ({resp.status_code}); canopy does not follow")
    try:
        body = _read(resp)
    except OutboundError:
        body = {}
    return resp.status_code, body, resp.headers
