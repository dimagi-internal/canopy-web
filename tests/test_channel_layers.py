"""Production's channel layer decides whether canopy-web can run more than one process.
These pin the choice, and why it is the pub/sub layer and not channels_redis's default."""
import pytest

from config.channel_layers import PREFIX, channel_layers


def test_with_redis_it_is_the_pub_sub_layer_with_health_checks_and_its_own_prefix():
    layer = channel_layers("redis://cache.example:6379/1")["default"]
    # Not channels_redis.core.RedisChannelLayer: its blocking pops tore sockets down here.
    assert layer["BACKEND"] == "channels_redis.pubsub.RedisPubSubChannelLayer"
    host = layer["CONFIG"]["hosts"][0]
    assert host["address"] == "redis://cache.example:6379/1"
    assert host["health_check_interval"] and host["socket_keepalive"] and host["retry_on_timeout"]
    # Pub/sub channels ignore the database index, and this Redis is shared with other services.
    assert layer["CONFIG"]["prefix"] == PREFIX == "canopy-web"


def test_memory_is_the_escape_hatch_and_the_default_without_redis():
    memory = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    assert channel_layers("redis://cache.example:6379/1", "memory") == memory
    assert channel_layers("", "") == memory
    assert channel_layers("redis://x/1", " Redis ")["default"]["BACKEND"].endswith("RedisPubSubChannelLayer")
    with pytest.raises(ValueError, match="CHANNEL_LAYER"):
        channel_layers("redis://x/1", "kafka")


def test_the_layer_it_names_can_be_built():
    from channels_redis.pubsub import RedisPubSubChannelLayer

    config = channel_layers("redis://127.0.0.1:1/1")["default"]["CONFIG"]
    layer = RedisPubSubChannelLayer(**config)  # builds without connecting
    assert layer is not None
