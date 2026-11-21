"""A relay host: the site carries a person's conversation with a canopy agent
server-to-server (``canopy_sdk.host.relay``).

What these pin: the token is minted for the HOST's own subject with the channel and
the agent signed in, cached until five minutes before it expires, re-minted once on
a 401; a send names the relay source; canopy's refusal code reaches the caller.
"""
from __future__ import annotations

import datetime as dt

import jwt
import pytest
from canopy_sdk import contract
from canopy_sdk.host import MintFailed, RelayClient, RelayError, sign_visitor_assertion
from jwt import PyJWK

from .helpers import CANOPY, World


class Canopy:
    """canopy's contact routes, in memory."""

    def __init__(self, world, *, ttl=30 * 60):
        self.world = world
        self.ttl = ttl
        self.now = 1_000_000.0
        self.minted: list[dict] = []
        self.calls: list[tuple] = []
        self.answers: dict[tuple, list] = {}

    def mint(self, config, payload):
        claims = jwt.decode(payload["assertion"], PyJWK.from_dict(self.world.config.public_jwk).key,
                            algorithms=["EdDSA"], audience=config.audience)
        self.minted.append({"claims": claims, "agent_slug": payload["agent_slug"]})
        expires = dt.datetime.fromtimestamp(self.now + self.ttl, tz=dt.UTC)
        return {"token": f"ccon_{len(self.minted)}", "expires_at": expires.isoformat(), "kind": "contact"}

    def transport(self, method, url, body, headers):
        path = url[len(CANOPY):]
        self.calls.append((method, path, body, headers["Authorization"]))
        queued = self.answers.get((method, path.split("?")[0]))
        if queued:
            return queued.pop(0)
        if method == "POST" and path == "/api/contact/sessions":
            return 200, {"id": "s-1", "agent_slug": body["agent_slug"]}
        if method == "POST" and path.endswith("/send"):
            return 200, {"turn_id": "t-1", "message_id": 7}
        if method == "GET" and path.startswith("/api/contact/turns/"):
            return 200, {"id": path.rsplit("/", 1)[-1], "status": "done"}
        return 404, {"detail": "not found"}


@pytest.fixture
def world():
    return World()


@pytest.fixture
def canopy(world):
    return Canopy(world)


@pytest.fixture
def relay(world, canopy):
    return RelayClient(world.config, transport=canopy.transport, mint=canopy.mint, clock=lambda: canopy.now)


def test_the_assertion_carries_the_channel_and_binds_the_agent(relay, canopy):
    relay.token_for("personalid:jdoe", name="J Doe", channel="personalid", agent_slug="coach")
    claims = canopy.minted[0]["claims"]
    assert claims["sub"] == "personalid:jdoe" and claims["name"] == "J Doe"
    assert claims[contract.CHANNEL_CLAIM] == "personalid"
    assert claims[contract.AGENT_CLAIM] == "coach" and canopy.minted[0]["agent_slug"] == "coach"


def test_workspace_names_the_tenant_for_one_token(relay, canopy):
    relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach", workspace="connect")
    assert canopy.minted[0]["claims"][contract.WORKSPACE_CLAIM] == "connect"


def test_a_token_is_cached_until_five_minutes_before_it_expires(relay, canopy):
    first = relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach")
    canopy.now += 24 * 60
    assert relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach") == first
    assert len(canopy.minted) == 1
    canopy.now += 2 * 60  # inside the last five minutes
    assert relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach") != first
    assert len(canopy.minted) == 2


def test_tokens_are_per_person_channel_and_agent(relay, canopy):
    relay.token_for("personalid:a", channel="personalid", agent_slug="coach")
    relay.token_for("personalid:b", channel="personalid", agent_slug="coach")
    relay.token_for("personalid:a", channel="sms", agent_slug="coach")
    relay.token_for("personalid:a", channel="personalid", agent_slug="other")
    assert len(canopy.minted) == 4


@pytest.mark.parametrize("channel", ["", "PersonalID", "has space", "x" * 33])
def test_a_relay_names_a_well_formed_channel(relay, channel):
    with pytest.raises(ValueError):
        relay.token_for("personalid:jdoe", channel=channel, agent_slug="coach")


def test_open_send_turn(relay, canopy):
    sid = relay.open_session("personalid:jdoe", agent_slug="coach", channel="personalid",
                             title="Coaching", metadata={"x_thread": "9"})
    assert sid == "s-1"
    assert canopy.calls[0][2] == {"agent_slug": "coach", "title": "Coaching", "metadata": {"x_thread": "9"}}
    out = relay.send(sid, "personalid:jdoe", "hello", client_id="m-1")
    assert out == {"turn_id": "t-1", "message_id": 7}
    method, path, body, auth = canopy.calls[1]
    assert (method, path) == ("POST", "/api/contact/sessions/s-1/send")
    assert body == {"text": "hello", "origin": contract.RELAY_ORIGIN, "client_id": "m-1"}
    assert auth == "Bearer ccon_1", "the token minted for that person"
    assert relay.turn("t-1", "personalid:jdoe")["status"] == "done"
    assert len(canopy.minted) == 1


def test_a_fresh_client_uses_its_defaults(world, canopy):
    relay = RelayClient(world.config, channel="personalid", agent_slug="coach",
                        transport=canopy.transport, mint=canopy.mint, clock=lambda: canopy.now)
    relay.send("s-9", "personalid:jdoe", "hi")
    assert canopy.minted[0]["claims"][contract.CHANNEL_CLAIM] == "personalid"


def test_without_a_token_or_defaults_it_says_so(relay):
    with pytest.raises(RelayError) as exc:
        relay.send("s-9", "personalid:jdoe", "hi")
    assert exc.value.code == "no_token"


def test_a_401_mints_again_once(relay, canopy):
    relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach")
    canopy.answers[("POST", "/api/contact/sessions/s-1/send")] = [(401, {"detail": "expired"})]
    assert relay.send("s-1", "personalid:jdoe", "hi")["turn_id"] == "t-1"
    assert len(canopy.minted) == 2
    assert canopy.calls[-1][3] == "Bearer ccon_2"


def test_canopys_refusal_code_reaches_the_caller(relay, canopy):
    relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach")
    canopy.answers[("POST", "/api/contact/sessions/s-1/send")] = [
        (429, {"detail": "rate_limited: too many messages (6 a minute)"})]
    with pytest.raises(RelayError) as exc:
        relay.send("s-1", "personalid:jdoe", "hi")
    assert exc.value.code == "rate_limited" and exc.value.status == 429


def test_a_refused_mint_is_a_relay_error(world):
    def refuse(config, payload):
        raise MintFailed('canopy returned 400: {"detail": "bad_channel: canopy_channel must match"}',
                         status=400)

    relay = RelayClient(world.config, transport=lambda *a: (200, {}), mint=refuse)
    with pytest.raises(RelayError) as exc:
        relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach")
    assert exc.value.code == "bad_channel" and exc.value.status == 400


def test_messages_pages_back_and_filters_after(relay, canopy):
    relay.token_for("personalid:jdoe", channel="personalid", agent_slug="coach")
    rows = [{"turn_index": i, "text": str(i)} for i in range(5)]
    canopy.answers[("GET", "/api/contact/sessions/s-1/messages")] = [
        (200, {"messages": rows[3:], "has_more_before": True}),
        (200, {"messages": rows[:3], "has_more_before": False}),
    ]
    assert [m["turn_index"] for m in relay.messages("s-1", "personalid:jdoe")] == [0, 1, 2, 3, 4]
    canopy.answers[("GET", "/api/contact/sessions/s-1/messages")] = [
        (200, {"messages": rows[3:], "has_more_before": True}),
        (200, {"messages": rows[:3], "has_more_before": False}),
    ]
    assert [m["turn_index"] for m in relay.messages("s-1", "personalid:jdoe", after=2)] == [3, 4]
    assert canopy.answers[("GET", "/api/contact/sessions/s-1/messages")] == [], \
        "a page reaching back past `after` ends the walk"


def test_the_channel_claim_is_signed_only_when_given(world):
    claims = jwt.decode(sign_visitor_assertion(world.config, "42"),
                        PyJWK.from_dict(world.config.public_jwk).key, algorithms=["EdDSA"],
                        audience=world.config.audience)
    assert contract.CHANNEL_CLAIM not in claims
    with pytest.raises(ValueError):
        sign_visitor_assertion(world.config, "42", channel="Bad Channel")


@pytest.mark.parametrize("value,expected", [(None, ""), ("", ""), ("personalid", "personalid"),
                                            ("wa_2-b", "wa_2-b")])
def test_parse_channel_claim(value, expected):
    assert contract.parse_channel_claim(value) == expected


@pytest.mark.parametrize("value", [3, "UPPER", "a b", "x" * 33, "é"])
def test_parse_channel_claim_refuses(value):
    with pytest.raises(ValueError):
        contract.parse_channel_claim(value)
