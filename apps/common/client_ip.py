"""The caller's address — the ONE reader of `X-Forwarded-For`.

canopy runs behind exactly one proxy that matters: the AWS ALB
(canopy.dimagi.com is a DNS-only CNAME to it; no Cloudflare in front). The ALB
APPENDS the address it saw to whatever `X-Forwarded-For` the client sent, so the
header's FIRST entry is whatever the client chose to write and only the LAST one
is the ALB's own observation. Three hand-rolled parsers took the first entry,
which let any caller pick the key of a per-address rate limit (MCP OAuth client
registration, the contact-token mint, the request-access form) and the address
recorded on an audit row.

`CANOPY_TRUSTED_PROXY_HOPS` is how many proxies append in front of Django (1 =
the ALB alone). With N hops the client is the N-th entry from the end; a header
shorter than that did not come through all of them, so it is not trusted and
`REMOTE_ADDR` answers.

The MCP in-process path forwards the MCP client's `X-Forwarded-For` into the
ASGI scope it builds (`apps/mcp/api_tools.FORWARDED_HEADERS`). That header is the
one the ALB already appended to on the MCP request itself, so reading its last
entry here yields the same real address, not a spoofable one.

`tests/test_client_ip_is_read_once.py` fails the build on any other reader.
"""
from __future__ import annotations

from django.conf import settings


def _hops() -> int:
    try:
        return max(int(getattr(settings, "CANOPY_TRUSTED_PROXY_HOPS", 1)), 1)
    except (TypeError, ValueError):
        return 1


def from_headers(forwarded: str | None, remote: str | None) -> str:
    """The client address from a raw `X-Forwarded-For` value and the socket's
    peer address; "" when neither says anything."""
    entries = [e.strip() for e in (forwarded or "").split(",") if e.strip()]
    hops = _hops()
    if len(entries) >= hops:
        return entries[-hops]
    return (remote or "").strip()


def client_ip(request) -> str:
    """The client address of a Django request; "" when unknown."""
    if request is None:
        return ""
    meta = getattr(request, "META", {}) or {}
    return from_headers(meta.get("HTTP_X_FORWARDED_FOR"), meta.get("REMOTE_ADDR"))


def from_scope(scope) -> str:
    """The client address of an ASGI scope (a WebSocket); "" when unknown."""
    forwarded = ""
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() == "x-forwarded-for":
            forwarded = value.decode("latin-1", "replace")
    client = scope.get("client") or ("", 0)
    return from_headers(forwarded, client[0] if client else "")
