"""A host's attested context on one message (``canopy_sdk.attested_context``).

What these pin: canopy keeps a context only from the site's own key, only for the
conversation and the person it names, once, briefly, and within its bounds.
"""
from __future__ import annotations

import json
import time

import pytest

from canopy_sdk import attested_context as ctx
from canopy_sdk.host import RelayClient
from canopy_sdk.keys import generate_private_key

from .helpers import World

SITE = "connect-labs"


@pytest.fixture
def key():
    return generate_private_key("EdDSA")


def _verify(token, key, **kw):
    args = {"site": SITE, "sub": "personalid:jdoe", "session": "s-1", **kw}
    return ctx.verify_context(token, [key.public_key()], **args)


def _sign(key, **kw):
    args = {"sub": "personalid:jdoe", "session": "s-1", "format": "markdown", "body": "Stage: **learn**", **kw}
    return ctx.sign_context(SITE, key, **args)


def test_a_round_trip(key):
    claims = _verify(_sign(key), key)
    assert (claims["format"], claims["body"]) == ("markdown", "Stage: **learn**")
    assert claims["exp"] - claims["iat"] == ctx.CONTEXT_MAX_LIFETIME


@pytest.mark.parametrize("field,value", [("sub", "personalid:other"), ("session", "s-2")])
def test_another_person_or_conversation_is_a_mismatch(key, field, value):
    with pytest.raises(ctx.ContextError) as exc:
        _verify(_sign(key), key, **{field: value})
    assert exc.value.code == "mismatch"


def test_only_the_sites_key_verifies(key):
    with pytest.raises(ctx.ContextError) as exc:
        _verify(_sign(generate_private_key("EdDSA")), key)
    assert exc.value.code == "bad_signature"


def test_another_site_is_refused(key):
    with pytest.raises(ctx.ContextError) as exc:
        _verify(_sign(key), key, site="ace-web")
    assert exc.value.code == "bad_issuer"


def test_it_is_spent_once(key):
    seen = set()

    def spend(claims):
        if claims["jti"] in seen:
            raise ctx.ContextError("replayed", "again")
        seen.add(claims["jti"])

    token = _sign(key)
    _verify(token, key, spend_jti=spend)
    with pytest.raises(ctx.ContextError) as exc:
        _verify(token, key, spend_jti=spend)
    assert exc.value.code == "replayed"


def test_a_stale_one_is_refused(key):
    with pytest.raises(ctx.ContextError) as exc:
        _verify(_sign(key, now=time.time() - 600), key)
    assert exc.value.code == "expired"


def test_another_typ_is_refused(key):
    from canopy_sdk.jose import sign

    token = sign({"iss": SITE, "sub": "personalid:jdoe", "session": "s-1", "iat": int(time.time()),
                  "exp": int(time.time()) + 30, "jti": "j", "format": "markdown", "body": "x"},
                 key, headers={"typ": "canopy-resources-updated+jwt"})
    with pytest.raises(ctx.ContextError) as exc:
        _verify(token, key)
    assert exc.value.code == "bad_typ"


@pytest.mark.parametrize("fmt,body,code", [
    ("html", "x", "bad_format"),
    ("markdown", "", "bad_body"),
    ("json", "{not json", "bad_body"),
    ("markdown", "x" * (ctx.MAX_CONTEXT_BODY + 1), "too_large"),
])
def test_bounds(key, fmt, body, code):
    with pytest.raises(ctx.ContextError) as exc:
        _sign(key, format=fmt, body=body)
    assert exc.value.code == code


def test_the_longest_body_verifies(key):
    body = "x" * ctx.MAX_CONTEXT_BODY
    assert _verify(_sign(key, body=body), key)["body"] == body


def test_the_relay_signs_a_context_for_its_send():
    world = World()
    sent = {}

    def transport(method, url, body, headers):
        sent.update(body or {})
        return 200, {"turn_id": "t-1", "message_id": 1}

    relay = RelayClient(world.config, channel="personalid", agent_slug="coach", transport=transport,
                        mint=lambda config, payload: {"token": "ccon_1", "expires_at": "2999-01-01T00:00:00+00:00"})
    relay.send("s-1", "personalid:jdoe", "hi", context={"format": "json", "body": {"stage": "learn"}})
    claims = ctx.verify_context(sent["context"], [world.config.signing_key.public_key()],
                                site="connect-labs", sub="personalid:jdoe", session="s-1")
    assert claims["format"] == "json" and json.loads(claims["body"]) == {"stage": "learn"}
