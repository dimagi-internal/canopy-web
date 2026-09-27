"""Fetching a document from a URL somebody else wrote down — without SSRF.

A host fetches canopy's Client ID Metadata Document and the JWKS it names; a
conformance run fetches a host's metadata. Each is a server-side request to an
address that came from configuration or from another document, made from inside
a network that usually has a metadata service on it. So every fetch here:

* is ``https`` only, on the default port, with no credentials in the URL;
* resolves the host ITSELF and refuses if ANY address it resolves to is private,
  loopback, link-local (the cloud metadata endpoint lives there), multicast,
  reserved, unspecified or otherwise not globally routable — and then CONNECTS
  TO THE ADDRESS IT VETTED, not to a second lookup, so DNS cannot answer
  differently between the check and the connection (rebinding). TLS is still
  verified against the hostname;
* follows no redirects (the cheapest way around a host check), times out
  quickly, and reads a bounded number of bytes.

Standard library only (``http.client`` + ``ssl``), so the core SDK needs no HTTP
client. Extracted from connect-labs ``connect_labs/mcp/client_metadata.py``,
which did the same with ``httpcore``.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlencode, urlsplit

TIMEOUT_SECONDS = 5
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_URL_LENGTH = 2048


class FetchError(Exception):
    """Why a document could not, or would not, be fetched. Safe to log: it never
    contains a credential."""


def address_allowed(address: str) -> bool:
    """Whether ``address`` is a publicly routable IP. IPv4-mapped IPv6 is judged
    as the IPv4 it maps."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified or not ip.is_global
    )


def vet_url(url: str) -> tuple[str, int]:
    """The host and port of ``url`` if it may be fetched, else ``FetchError``.

    An IP literal is judged here; a name is judged at connect time, on every
    address it resolves to.
    """
    if not isinstance(url, str) or len(url) > MAX_URL_LENGTH or any(c.isspace() for c in url):
        raise FetchError("not a URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("not a URL") from exc
    if parts.scheme != "https":
        raise FetchError("only https URLs are fetched")
    if not parts.hostname or parts.username or parts.password:
        raise FetchError("the URL must name a host and carry no credentials")
    if port not in (None, 443):
        raise FetchError("only the default https port is fetched")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        if not address_allowed(parts.hostname):
            raise FetchError("that address is not public")
    return parts.hostname, 443


def resolve_public(host: str, port: int = 443) -> str:
    """Resolve ``host`` and return ONE vetted address — or refuse if ANY
    address it resolves to is not public (a name resolving to one public and one
    private address is exactly the shape a rebinding attack takes)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError(f"could not resolve {host}") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise FetchError(f"{host} resolved to nothing")
    if not all(address_allowed(a) for a in addresses):
        raise FetchError(f"{host} resolves to an address that is not public")
    return addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an address vetted in advance while doing TLS (SNI and
    certificate verification) against the hostname."""

    def __init__(self, host: str, address: str, *, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):  # noqa: D401 - http.client API
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _request(method: str, url: str, *, body: bytes | None, headers: dict,
             timeout: float, max_bytes: int) -> tuple[int, bytes, dict]:
    host, _ = vet_url(url)
    address = resolve_public(host)
    parts = urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    conn = _PinnedHTTPSConnection(host, address, timeout=timeout)
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FetchError(f"{url} answered with more than {max_bytes} bytes")
        return resp.status, data, {k: v for k, v in resp.getheaders()}
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError(f"{url} could not be fetched ({type(exc).__name__})") from exc
    finally:
        conn.close()


def _json(url: str, data: bytes) -> dict:
    try:
        document = json.loads(data or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FetchError(f"{url} is not JSON") from exc
    if not isinstance(document, dict):
        raise FetchError(f"{url} is not a JSON object")
    return document


def get_json(url: str, *, timeout: float = TIMEOUT_SECONDS,
             max_bytes: int = MAX_DOCUMENT_BYTES) -> dict:
    """GET a JSON object. Anything but a 200 is a ``FetchError`` (a redirect
    included — it is never followed)."""
    status, data, _ = _request("GET", url, body=None, headers={"Accept": "application/json"},
                               timeout=timeout, max_bytes=max_bytes)
    if status != 200:
        raise FetchError(f"{url} answered {status}")
    return _json(url, data)


def post_form(url: str, data: dict, *, headers: dict | None = None,
              timeout: float = TIMEOUT_SECONDS,
              max_bytes: int = MAX_DOCUMENT_BYTES) -> tuple[int, dict, dict]:
    """POST a form. Returns ``(status, json_body, response_headers)`` — OAuth
    errors are read from the body, so a non-200 is not raised. A redirect is."""
    body = urlencode(data).encode()
    status, raw, resp_headers = _request(
        "POST", url, body=body,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        timeout=timeout, max_bytes=max_bytes)
    if 300 <= status < 400:
        raise FetchError(f"{url} redirected ({status}); redirects are not followed")
    try:
        doc = _json(url, raw)
    except FetchError:
        doc = {}
    return status, doc, resp_headers


def post_json(url: str, payload, *, headers: dict | None = None,
              timeout: float = TIMEOUT_SECONDS,
              max_bytes: int = MAX_DOCUMENT_BYTES) -> tuple[int, bytes, dict]:
    """POST a JSON body; returns the raw response (MCP answers JSON or SSE)."""
    status, raw, resp_headers = _request(
        "POST", url, body=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        timeout=timeout, max_bytes=max_bytes)
    if 300 <= status < 400:
        raise FetchError(f"{url} redirected ({status}); redirects are not followed")
    return status, raw, resp_headers
