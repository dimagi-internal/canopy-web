"""Which Channels layer a deployment uses, decided in one tested place.

With a Redis URL the layer is channels_redis's PUB/SUB layer, so any number of web tasks
and uvicorn workers can serve sockets: a runner's control channel on one task hears a
wake published by another, and a phone and a laptop on different tasks reach each other
(apps/listen).

Why pub/sub and not channels_redis's default layer: the default one has every consumer
block on a Redis pop (BZPOPMIN, 5 s). On the shared ElastiCache an idle pop raised
`TimeoutError: Timeout reading from …` up through `await_many_dispatch`, which tore the
WebSocket down: ~5-10 s drops on every realtime surface, until production fell back to
the in-memory layer and, with it, a single process. In the pub/sub layer a consumer
awaits a local asyncio queue; one background reader per process polls the subscription
with a 0.1 s timeout and logs and retries any Redis error, and redis-py re-subscribes
after a reconnect. A Redis hiccup can delay a message, but can't close a socket.

Pub/sub delivers at most once, to whoever is subscribed right then. Everything canopy-web
sends over the layer is that kind of message: a wake (the poll is the backstop), a stream
notice, a listen frame (a phone holds its audio until the laptop says `ready`).

The prefix: Redis pub/sub channels are global to the server, whatever the database index
in the URL, and this ElastiCache is shared with ace-web and connect-labs. Without our own
prefix, two services' group names could cross.

`CHANNEL_LAYER=memory` is the escape hatch back to one process.
"""
from __future__ import annotations

PREFIX = "canopy-web"


def channel_layers(redis_url: str, choice: str = "") -> dict:
    choice = (choice or "").strip().lower()
    if choice == "memory" or not redis_url:
        return {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    if choice not in ("", "redis"):
        raise ValueError(f"CHANNEL_LAYER must be 'redis' or 'memory', not {choice!r}")
    return {
        "default": {
            "BACKEND": "channels_redis.pubsub.RedisPubSubChannelLayer",
            "CONFIG": {
                "hosts": [{
                    "address": redis_url,
                    # Ping an idle connection before using it, keep TCP alive, and retry a
                    # read that times out instead of failing it.
                    "health_check_interval": 30,
                    "socket_keepalive": True,
                    "retry_on_timeout": True,
                }],
                "prefix": PREFIX,
            },
        }
    }
