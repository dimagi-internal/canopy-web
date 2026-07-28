"""WebSocket URL routes for the realtime app.

turn_id is matched loosely (hex + dashes) and parsed/validated in the consumer,
which 4004s an unparsable id rather than 404ing at the router.
"""
from django.urls import path, re_path

from . import consumers
from .presence_consumer import PresenceConsumer

websocket_urlpatterns = [
    re_path(r"^ws/turns/(?P<turn_id>[0-9a-fA-F-]+)/$", consumers.TurnConsumer.as_asgi()),
    path("ws/supervisor/", consumers.SupervisorConsumer.as_asgi()),
    re_path(r"^ws/runner/(?P<runner_id>[0-9a-fA-F-]+)/$", consumers.RunnerConsumer.as_asgi()),
    path("ws/presence/", PresenceConsumer.as_asgi()),
]
