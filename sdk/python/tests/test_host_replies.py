"""Reply push, the host's half (``canopy_sdk.host.replies``).

What these pin: a host believes a reply only from the canopy it trusts (its client
metadata's key), only addressed to its own reply URL and its own site name, briefly,
and once.
"""
from __future__ import annotations

import time

import pytest

from canopy_sdk.host import HostConfig, ReplyRefused, verify_reply
from canopy_sdk.host.replies import REPLY_TYP, sign_reply
from canopy_sdk.keys import generate_private_key, private_pem
from canopy_sdk.stores import MemoryJtiStore

from .helpers import CANOPY, CLIENT_ID, World

HOOK = "https://labs.example.org/canopy/replies/"


@pytest.fixture
def world():
    return World()


@pytest.fixture
def config(world):
    return HostConfig(signing_key=private_pem(world.host_key), canopy_base_url=CANOPY, app_name="connect-labs",
                      canopy_client_id=CLIENT_ID, reply_url=HOOK)


def _sign(world, *, key=None, aud=HOOK, **kw):
    args = {"sub": "personalid:jdoe", "site": "connect-labs", "session": "s-1", "turn": "t-1", "seq": 4,
            "text": "Friday.", "ended": True, **kw}
    return sign_reply(key or world.client.client_key, CLIENT_ID, aud, **args)


def _verify(world, config, token, **kw):
    return verify_reply(token, config=config, client_keys=world.resolver,
                        replay_store=kw.pop("replay_store", MemoryJtiStore()), **kw)


def test_a_round_trip(world, config):
    reply = _verify(world, config, _sign(world))
    assert (reply.site, reply.sub, reply.session, reply.turn, reply.seq) == (
        "connect-labs", "personalid:jdoe", "s-1", "t-1", 4)
    assert reply.text == "Friday." and reply.ended and reply.error == ""


def test_an_error_is_carried(world, config):
    assert _verify(world, config, _sign(world, text="", error="failed")).error == "failed"


def test_only_canopys_key(world, config):
    with pytest.raises(ReplyRefused) as exc:
        _verify(world, config, _sign(world, key=generate_private_key("EdDSA")))
    assert exc.value.code in ("unknown_key", "bad_signature")


def test_only_this_hosts_url(world, config):
    with pytest.raises(ReplyRefused) as exc:
        _verify(world, config, _sign(world, aud="https://elsewhere.example.org/r"))
    assert exc.value.code == "bad_audience"


def test_only_this_site(world, config):
    with pytest.raises(ReplyRefused) as exc:
        _verify(world, config, _sign(world, site="ace-web"))
    assert exc.value.code == "wrong_site"


def test_once(world, config):
    store = MemoryJtiStore()
    token = _sign(world)
    _verify(world, config, token, replay_store=store)
    with pytest.raises(ReplyRefused) as exc:
        _verify(world, config, token, replay_store=store)
    assert exc.value.code == "replayed"


def test_briefly(world, config):
    with pytest.raises(ReplyRefused) as exc:
        _verify(world, config, _sign(world, now=time.time() - 3600))
    assert exc.value.code == "expired"


def test_a_host_with_no_reply_url_trusts_none(world):
    bare = HostConfig(signing_key=private_pem(world.host_key), app_name="connect-labs",
                      canopy_client_id=CLIENT_ID)
    with pytest.raises(ReplyRefused) as exc:
        verify_reply(_sign(world), config=bare, client_keys=world.resolver)
    assert exc.value.code == "not_configured"
    assert verify_reply(_sign(world), config=bare, audience=HOOK, client_keys=world.resolver,
                        replay_store=MemoryJtiStore()).ended


def test_the_typ_is_its_own(world, config):
    import jwt

    token = _sign(world)
    header = jwt.get_unverified_header(token)
    assert header["typ"] == REPLY_TYP and header["alg"] == "EdDSA"
