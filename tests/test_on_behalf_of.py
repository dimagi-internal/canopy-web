"""Canopy vouching for a caller to the site that caller came from.

The mirror of `test_contact_assertions.py`: there canopy VERIFIES a host's
claim, here canopy MAKES one, and the same properties have to hold in the other
direction. An assertion canopy signs is a credential — whoever holds it can be
that person at that host — so the questions that matter are who can ask for one,
who it names, where it can be spent, and for how long.
"""

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents.models import Agent
from apps.contacts.models import Contact
from apps.harness.models import Turn
from apps.tokens import onbehalf
from apps.tokens.models import AppCredential
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _key() -> str:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


KEY = _key()
ON = dict(ONBEHALF_SIGNING_KEY=KEY, EMBED_ASSERTION_AUDIENCE="https://canopy.test")


def _world(*, external_id="u-42", with_app=True):
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws,
                                       role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    app = None
    if with_app:
        _raw, app = AppCredential.create_credential(name="connect-labs", domains=[],
                                                    created_by=owner)
    contact = Contact.objects.create(
        workspace=ws, app=app, external_id=external_id if with_app else "",
        email="" if with_app else "someone@partner.org",
    )
    turn = Turn.objects.create(agent=agent, prompt="hi", initiator_contact=contact,
                               initiator_kind="contact", capability="ask")
    return turn, agent, contact


@override_settings(**ON)
def test_it_names_the_caller_by_the_hosts_own_id_addressed_only_to_that_host():
    turn, _agent, _contact = _world()
    out = onbehalf.mint(turn, agent_slug="echo")
    claims = jwt.decode(out["assertion"], onbehalf.public_jwk_pem(),
                        algorithms=["EdDSA"], audience="connect-labs")
    assert claims["sub"] == "u-42"          # THEIR id, not canopy's
    assert claims["aud"] == "connect-labs"  # one host, not "any site the agent calls next"
    assert claims["iss"] == "https://canopy.test"
    assert claims["act"] == {"sub": "agent:echo"}
    assert claims["exp"] - claims["iat"] == onbehalf.TTL_SECONDS <= 120


@override_settings(**ON)
def test_it_expires_in_two_minutes_so_a_copy_stops_working(monkeypatch):
    turn, _a, _c = _world()
    real = onbehalf.time.time
    monkeypatch.setattr(onbehalf.time, "time", lambda: real() - 300)
    out = onbehalf.mint(turn, agent_slug="echo")
    monkeypatch.undo()
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(out["assertion"], onbehalf.public_jwk_pem(), algorithms=["EdDSA"],
                   audience="connect-labs")


@override_settings(**ON)
def test_another_hosts_key_cannot_verify_it():
    """`aud` bounds where it is ACCEPTED; the signature bounds who could have
    written it. A host must not be able to mint one canopy would be blamed for."""
    turn, _a, _c = _world()
    out = onbehalf.mint(turn, agent_slug="echo")
    other = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(out["assertion"], other, algorithms=["EdDSA"], audience="connect-labs")


@override_settings(**ON)
def test_a_caller_from_no_connected_site_is_refused_not_described_some_other_way():
    """An email correspondent has no account at any host, so there is nobody to
    act as. Asserting their ADDRESS instead would push the mapping back onto the
    host — the guess this exists to remove."""
    turn, _a, _c = _world(with_app=False)
    with pytest.raises(onbehalf.OnBehalfError) as exc:
        onbehalf.mint(turn, agent_slug="echo")
    assert "connected site" in str(exc.value)


@override_settings(**ON)
def test_a_turn_with_no_caller_is_refused():
    turn, _a, _c = _world()
    Turn.objects.filter(pk=turn.pk).update(initiator_contact=None)
    turn.refresh_from_db()
    with pytest.raises(onbehalf.OnBehalfError):
        onbehalf.mint(turn, agent_slug="echo")


@override_settings(ONBEHALF_SIGNING_KEY="")
def test_an_unconfigured_canopy_says_so_rather_than_signing_something_weaker():
    turn, _a, _c = _world()
    assert onbehalf.configured() is False
    with pytest.raises(onbehalf.OnBehalfError) as exc:
        onbehalf.mint(turn, agent_slug="echo")
    assert "signing key" in str(exc.value)


@override_settings(**ON)
def test_the_jwks_endpoint_publishes_the_public_half_and_nothing_else():
    """A verifier must be able to fetch this before it trusts anything, so it is
    open — and it must never carry the private half."""
    r = Client().get("/api/tokens/on-behalf-of/jwks")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert len(keys) == 1
    assert keys[0]["kty"] == "OKP" and keys[0]["alg"] == "EdDSA"
    assert "d" not in keys[0]              # the private scalar
    assert "PRIVATE" not in r.content.decode()


@override_settings(ONBEHALF_SIGNING_KEY="PLACEHOLDER")
def test_the_cfn_secrets_birth_value_reads_as_unconfigured_not_as_a_key():
    """A secret container is born holding "PLACEHOLDER". Treating that as a key
    would make `configured()` say yes and every signature die inside the crypto
    library, instead of the clean refusal the agent is meant to get."""
    turn, _a, _c = _world()
    assert onbehalf.configured() is False
    with pytest.raises(onbehalf.OnBehalfError):
        onbehalf.mint(turn, agent_slug="echo")
    assert Client().get("/api/tokens/on-behalf-of/jwks").json() == {"keys": []}


@override_settings(ONBEHALF_SIGNING_KEY="")
def test_an_unconfigured_deployment_publishes_an_empty_key_set_not_an_error():
    r = Client().get("/api/tokens/on-behalf-of/jwks")
    assert r.status_code == 200 and r.json() == {"keys": []}


def test_the_published_key_actually_verifies_what_canopy_signs():
    """Two settings that must agree is a way to publish a key that verifies
    nothing — and the failure would show up at the host, days later."""
    from jwt import PyJWK

    with override_settings(**ON):
        turn, _a, _c = _world()
        out = onbehalf.mint(turn, agent_slug="echo")
        jwk = Client().get("/api/tokens/on-behalf-of/jwks").json()["keys"][0]
    claims = jwt.decode(out["assertion"], PyJWK.from_dict(jwk).key,
                        algorithms=["EdDSA"], audience="connect-labs")
    assert claims["sub"] == "u-42"


def test_the_tool_is_registered_on_the_mounted_server():
    """A decorator that never reaches the server is a tool nobody can call, and
    nothing else in the chain would say so."""
    from asgiref.sync import async_to_sync

    from apps.mcp.server import mcp

    names = {t.name for t in async_to_sync(mcp.list_tools)()}
    assert "act_on_behalf_of_caller" in names


def test_only_a_callers_own_conversation_may_be_vouched_for():
    """The tool takes a turn id, so without pinning the ARGUMENT is the whole
    gate: one caller could be vouched for as another. Same rule as
    `who_is_asking`, and it mints a credential, so it matters more."""
    from apps.mcp.turn_scope import TURN_PINNED

    assert "act_on_behalf_of_caller" in TURN_PINNED
