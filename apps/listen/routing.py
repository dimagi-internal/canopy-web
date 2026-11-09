"""WebSocket route for the listen relay. The id is matched loosely and parsed in the consumer."""
from django.urls import re_path

from .consumers import ListenConsumer

websocket_urlpatterns = [
    re_path(r"^ws/listen/(?P<session_id>[0-9a-fA-F-]+)/$", ListenConsumer.as_asgi()),
]
