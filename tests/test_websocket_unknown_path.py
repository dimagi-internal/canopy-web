"""A WebSocket path no route matches closes 4004 instead of raising a handshake 500.

The production router raised `ValueError("No route found")` for
`ws/canopy-sessions/<uuid>./` (a session link that kept a sentence-ending period),
and the client retried the 500 every few seconds for hours. A 4004 close is the
answer the chat client stops on.
"""
from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator

pytestmark = pytest.mark.django_db(transaction=True)


def _open(path):
    from config.asgi import _websocket_urlpatterns

    async def go():
        comm = WebsocketCommunicator(URLRouter(_websocket_urlpatterns), path)
        connected, _ = await comm.connect()
        out = await comm.receive_output() if connected else None
        await comm.wait()
        return connected, out
    return async_to_sync(go)()


@pytest.mark.parametrize("path", [
    "/ws/canopy-sessions/22a84675-a050-4e8b-988c-b19e2d75c385./",
    "/ws/no-such-socket/",
])
def test_unknown_socket_path_is_a_4004_close(path):
    connected, out = _open(path)
    assert connected
    assert out == {"type": "websocket.close", "code": 4004}


def test_the_catch_all_is_last_so_real_routes_still_match():
    from config.asgi import _websocket_urlpatterns
    from apps.realtime.unknown_path import UnknownPathConsumer

    callbacks = [getattr(p.callback, "consumer_class", None) for p in _websocket_urlpatterns]
    assert callbacks.index(UnknownPathConsumer) == len(callbacks) - 1
