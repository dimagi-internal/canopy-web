"""SP1 Task 8 — websocket routing resolves to the right consumers, and the
combined ASGI application builds with the Channels websocket wiring in place."""
from __future__ import annotations

import uuid

from apps.realtime.consumers import RunnerConsumer, SupervisorConsumer, TurnConsumer
from apps.realtime.presence_consumer import PresenceConsumer
from apps.realtime.routing import websocket_urlpatterns


def _match(path: str):
    for pat in websocket_urlpatterns:
        if pat.pattern.regex.match(path):
            # channels' as_asgi() stamps .consumer_class on the returned app.
            return pat.callback.consumer_class
    return None


def test_routes_registered():
    assert len(websocket_urlpatterns) == 4


def test_turn_route_resolves():
    assert _match(f"ws/turns/{uuid.uuid4().hex}/") is TurnConsumer


def test_runner_route_resolves():
    assert _match(f"ws/runner/{uuid.uuid4().hex}/") is RunnerConsumer


def test_supervisor_route_resolves():
    assert _match("ws/supervisor/") is SupervisorConsumer


def test_presence_route_resolves():
    assert _match("ws/presence/") is PresenceConsumer


def test_unknown_route_unmatched():
    assert _match("ws/nope/") is None


def test_asgi_application_builds():
    import config.asgi as asgi

    assert asgi.application is not None
