"""Fetching a connected site's public keys from the site itself.

**The point is that nobody copies a key.** A site pastes one PEM into canopy
today, which works exactly once: the day it rotates, every assertion it signs
is refused until somebody notices and pastes the new one. Rotation being
manual means rotation does not happen, which is the opposite of what a signing
key is for. A site that publishes a JWKS instead — the same thing every OIDC
provider already serves — rotates by publishing the new key beside the old,
switching its signer, and dropping the old one later, with no coordination and
no canopy change. Connecting a system becomes "give us your JWKS URL".

Keys are selected by `kid` when the assertion names one, which is what makes
two valid keys at once unambiguous. An unknown `kid` is the signal that the
site just rotated, so it forces ONE refetch — bounded, because a forged `kid`
would otherwise be a free way to make canopy fetch on demand.

**This is canopy making an outbound request to an operator-supplied URL**, so
the guards here are the point of the file, not decoration:

* **HTTPS only.** A key fetched over plaintext is a key an intermediary can
  replace, which makes the whole signature meaningless.
* **No private address space.** Canopy runs in a VPC with a metadata service
  and internal services on it; `http://169.254.169.254/...` as a "JWKS URL"
  would turn this into a credential reader. Every resolved address is checked,
  not just the hostname, because a public name can resolve anywhere.
* **No redirects.** A redirect is the cheapest way around a host check.
* **Bounded time and size**, so a hostile endpoint cannot hold a worker open or
  feed canopy an unbounded body.
* **Cached, and fail-closed.** A fetch failure falls back to the last good
  copy; with no copy, verification refuses rather than "succeeding" on nothing.

Residual, stated rather than hidden: a name that passes the check and then
resolves elsewhere on the connection (DNS rebinding) is not defeated here.
Closing it means pinning the resolved address into the connection, which
`requests` cannot express; the practical bound is that the response must also
parse as a JWKS and verify a signature, so a rebound host yields nothing
useful.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from urllib.parse import urlparse

import requests
from django.core.cache import cache

#: How long a good fetch is reused. Short enough that a rotation is picked up
#: on its own, long enough that canopy is not fetching per assertion.
CACHE_SECONDS = 600

#: How long the last good copy survives to be fallen back on when a fetch
#: FAILS. A separate, much longer entry than the fresh one above, and that
#: separation is the whole mechanism: one cache entry cannot do both jobs,
#: because the moment it expires there is nothing left to fall back to — the
#: fallback read would miss for exactly the same reason the first read did.
STALE_SECONDS = 24 * 60 * 60

#: How long "we just fetched and still did not find that kid" is remembered, so
#: an assertion naming a garbage kid cannot make canopy fetch per request.
MISS_SECONDS = 60

TIMEOUT_SECONDS = 5
MAX_BYTES = 64 * 1024


class JwksError(Exception):
    """Why a site's keys could not be fetched. The message reaches an operator."""


def _refuse_private(host: str) -> None:
    """Every address the host resolves to must be publicly routable."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise JwksError(f"{host!r} does not resolve ({exc})") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:  # pragma: no cover - getaddrinfo returned nonsense
            raise JwksError(f"{host!r} resolved to something that is not an address") from None
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            # Deliberately says WHICH address, because the honest cause is
            # usually a staging URL, not an attack.
            raise JwksError(
                f"{host!r} resolves to {addr}, which is inside private address space; "
                "a JWKS URL must be reachable from the public internet"
            )


def validate_url(url: str) -> str:
    """Check a URL before it is ever stored. Raises `JwksError` with the reason."""
    value = (url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise JwksError("a JWKS URL must be https — a key fetched in the clear is not a key")
    if not parsed.hostname:
        raise JwksError("that JWKS URL has no host")
    _refuse_private(parsed.hostname)
    return value


def _fetch(url: str) -> list[dict]:
    _refuse_private(urlparse(url).hostname or "")
    try:
        resp = requests.get(
            url,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
            headers={"Accept": "application/json"},
            stream=True,
        )
    except requests.RequestException as exc:
        raise JwksError(f"could not reach {url} ({exc})") from exc
    if resp.status_code != 200:
        raise JwksError(f"{url} answered {resp.status_code}")
    body = resp.raw.read(MAX_BYTES + 1, decode_content=True) or b""
    if len(body) > MAX_BYTES:
        raise JwksError(f"{url} returned more than {MAX_BYTES} bytes")
    try:
        doc = json.loads(body)
        keys = doc["keys"]
        assert isinstance(keys, list)
    except Exception as exc:  # noqa: BLE001
        raise JwksError(f"{url} did not return a JWKS document ({exc})") from exc
    return [k for k in keys if isinstance(k, dict)]


def _cache_key(url: str) -> str:
    return f"tokens:jwks:{url}"


def _stale_key(url: str) -> str:
    return f"tokens:jwks-stale:{url}"


def keys_for(url: str, *, kid: str = "", force: bool = False) -> list:
    """The site's public keys, as objects `jwt.decode` accepts.

    `kid` narrows to the one key the assertion names, and an unknown one forces
    a single refetch — a rotation looks exactly like this, and waiting out the
    cache would mean refusing valid assertions for ten minutes.
    """
    from jwt import PyJWK

    url = (url or "").strip()
    if not url:
        return []
    cached = None if force else cache.get(_cache_key(url))
    if cached is None:
        try:
            raw_keys = _fetch(url)
            cache.set(_cache_key(url), raw_keys, CACHE_SECONDS)
            cache.set(_stale_key(url), raw_keys, STALE_SECONDS)
        except JwksError:
            # Fall back to the last good copy: a site's key does not stop being
            # valid because its web server had a bad minute. It lives under its
            # OWN key with a longer life — reading the entry that just missed
            # would be a fallback that can never fire. With no copy at all
            # there is nothing to verify against, and the caller refuses.
            raw_keys = cache.get(_stale_key(url))
            if raw_keys is None:
                raise
    else:
        raw_keys = cached

    if kid:
        matching = [k for k in raw_keys if k.get("kid") == kid]
        if not matching and not force:
            # Either a rotation we have not seen, or a made-up kid. One refetch,
            # then remember the miss so a made-up kid is not a fetch per call.
            miss_key = f"tokens:jwks-miss:{url}:{kid}"
            if cache.get(miss_key) is None:
                cache.set(miss_key, 1, MISS_SECONDS)
                return keys_for(url, kid=kid, force=True)
            return []
        raw_keys = matching

    out = []
    for entry in raw_keys:
        try:
            out.append(PyJWK.from_dict(entry).key)
        except Exception:  # noqa: BLE001 - one malformed entry must not hide the rest
            continue
    return out
