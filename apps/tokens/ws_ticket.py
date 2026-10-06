"""One-time WebSocket tickets: keep a token out of the socket's URL.

A browser cannot put an `Authorization` header on `new WebSocket()`, so a page
that holds a delegated or contact token used to put the token itself on the URL
(`?token=`). URLs are written to access logs — the load balancer's and the
container's — so every chat a connected site opened left a live token in a log
line. A ticket is what goes there instead: the page trades its token for one
over REST (where the token rides a header), and opens the socket with
`?ticket=`. A ticket lives for TICKET_TTL_SECONDS and is spent by the first
socket that presents it, so one found in a log is already worthless.

The ticket names the token it was minted from and nothing else, and redeeming
it hands the WebSocket handshake that same token — so a ticketed socket gets
exactly the identity, connected site, runner requirements and assurance the
token itself would have given it, decided by the same code
(`apps.realtime.channels_auth`).

The raw token sits in the cache for at most TICKET_TTL_SECONDS, keyed by the
ticket's hash. The cache is the deployment's own Redis, inside the VPC; the
token is already in that process's memory for every request it authenticates.
"""
from __future__ import annotations

import hashlib
import secrets

from django.core.cache import cache

TICKET_TTL_SECONDS = 30
_PREFIX = "ws-ticket:"


def _key(ticket: str) -> str:
    return _PREFIX + hashlib.sha256(ticket.encode()).hexdigest()


def mint(raw_token: str) -> str:
    """A fresh single-use ticket standing for `raw_token`."""
    ticket = secrets.token_urlsafe(32)
    cache.set(_key(ticket), raw_token, timeout=TICKET_TTL_SECONDS)
    return ticket


def redeem(ticket: str) -> str | None:
    """The token a ticket stands for, once. None for an unknown, expired or spent
    ticket. Single use even under a race: only one caller's delete succeeds."""
    if not ticket:
        return None
    key = _key(ticket)
    raw = cache.get(key)
    if raw is None or not cache.delete(key):
        return None
    return raw


def bearer(request) -> str:
    """The raw bearer token on a request ("" when there is none)."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""
