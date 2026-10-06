"""A token never has to ride a socket URL: trade it for a one-time ticket.

URLs are written to access logs, and a browser cannot put a header on
`new WebSocket()` — so a connected site's chat put its delegated token, and the
widget its contact token, on `?token=`, leaving live tokens in log lines. A
ticket (apps/tokens/ws_ticket.py) goes there instead: minted over REST where
the token rides a header, good for one socket, for 30 seconds.
"""

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.test import Client

from apps.realtime.channels_auth import RealtimeAuthMiddleware
from apps.tokens import ws_ticket
from apps.tokens.models import DelegatedToken

from .test_contact_websocket import _contact_token, _world

pytestmark = [pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


async def _scope_after_auth(query: str, path="/ws/canopy-sessions/0b1e3c2a-6f1d-4c8e-9a51-2f3d4e5a6b7c/"):
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    await RealtimeAuthMiddleware(app)({"type": "websocket", "path": path, "headers": [],
                                       "query_string": query.encode()}, None, None)
    return seen


def test_a_ticket_works_once():
    t = ws_ticket.mint("raw-token")
    assert ws_ticket.redeem(t) == "raw-token"
    assert ws_ticket.redeem(t) is None
    assert ws_ticket.redeem("never-minted") is None
    assert ws_ticket.redeem("") is None


def test_a_site_trades_its_delegated_token_for_a_ticket():
    owner, _ws, app, _priv = _world()
    raw, _tok = DelegatedToken.issue(app=app, user=owner, ttl_seconds=900)
    resp = Client().post("/api/embed/ws-ticket", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["expires_in"] == ws_ticket.TICKET_TTL_SECONDS
    assert ws_ticket.redeem(body["ticket"]) == raw


def test_a_signed_in_browser_gets_no_ticket():
    """canopy's own pages sign their sockets in with the session cookie; there
    is no token to stand in for."""
    owner, *_ = _world()
    c = Client()
    c.force_login(owner)
    assert c.post("/api/embed/ws-ticket").status_code in (400, 403)


def test_a_contact_trades_its_token_for_a_ticket():
    _owner, _ws, _app, priv = _world()
    token = _contact_token(priv)
    resp = Client().post("/api/contact/ws-ticket", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert resp.status_code == 200, resp.content
    assert ws_ticket.redeem(resp.json()["ticket"]) == token


@pytest.mark.asyncio
async def test_a_ticketed_socket_is_the_token_holder_and_the_ticket_is_spent():
    owner, _ws, app, _priv = await sync_to_async(_world)()
    raw, _tok = await sync_to_async(DelegatedToken.issue)(app=app, user=owner, ttl_seconds=900)
    ticket = await sync_to_async(ws_ticket.mint)(raw)

    first = await _scope_after_auth(f"ticket={ticket}")
    assert first["user"].pk == owner.pk
    assert first["delegated_app"].pk == app.pk

    again = await _scope_after_auth(f"ticket={ticket}")
    assert not again["user"].is_authenticated


@pytest.mark.asyncio
async def test_a_ticketed_contact_socket_is_the_contact():
    _owner, _ws, _app, priv = await sync_to_async(_world)()
    token = await sync_to_async(_contact_token)(priv)
    ticket = await sync_to_async(ws_ticket.mint)(token)
    scope = await _scope_after_auth(f"ticket={ticket}")
    assert scope["contact"] is not None
    assert not scope["user"].is_authenticated


@pytest.mark.asyncio
async def test_a_bad_ticket_cannot_smuggle_a_token_beside_it():
    """A spent ticket must not fall back to a `token=` sent alongside it —
    otherwise the ticket check is decoration."""
    owner, _ws, app, _priv = await sync_to_async(_world)()
    raw, _tok = await sync_to_async(DelegatedToken.issue)(app=app, user=owner, ttl_seconds=900)
    scope = await _scope_after_auth(f"ticket=spent&token={raw}")
    assert not scope["user"].is_authenticated
