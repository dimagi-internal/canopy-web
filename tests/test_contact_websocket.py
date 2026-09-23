"""A contact listening to their own conversation.

The HTTP loop landed first, which left the useful half missing: without a
socket the agent's reply never streams, so a contact types and then watches
nothing happen.

They connect READ-ONLY. Presence, co-edited drafts and stop are multiplayer
features for members, and every one of them is keyed on a user id a contact
does not have — so the smallest correct thing is also the one they need.
"""

import datetime as dt
import uuid

import jwt
import pytest
from channels.testing import WebsocketCommunicator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.contrib.auth.models import AnonymousUser, User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session
from apps.contacts.models import Contact
from apps.tokens import assertions
from apps.tokens.models import AppCredential, AppCredentialTenant, AppCredentialAgent
from apps.canopy_sessions.consumers import SessionConsumer
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


def _keypair():
    priv = ed25519.Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


def _world():
    owner = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    priv, pub = _keypair()
    _raw, app = AppCredential.create_credential(name="connect-labs", created_by=owner)
    app.workspace = ws
    app.public_keys = [pub]
    app.save(update_fields=["workspace", "public_keys"])
    # The grant a real registration makes: a site acts for a tenant because
    # that tenant's owner said so, not because the row names a workspace.
    AppCredentialTenant.objects.create(app=app, workspace=ws, created_by=owner)
    AppCredentialAgent.objects.create(app=app, agent=agent)
    return owner, ws, app, priv


def _contact_token(priv, sub="u-42"):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    assertion = jwt.encode(
        {"iss": "connect-labs", "sub": sub, "aud": assertions.audience(),
         "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
        priv, algorithm="EdDSA",
    )
    return Client().post("/api/auth/contact-token", data={"assertion": assertion},
                         content_type="application/json").json()["token"]


def _start_session(token, agent_slug="echo"):
    return Client().post(
        "/api/contact/sessions", data={"agent_slug": agent_slug},
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
    ).json()["id"]


async def _connect(session_id, contact):
    """Drive the CONSUMER, with the scope the middleware would have built.

    Not the whole ASGI app: `AllowedHostsOriginValidator` sits in front of the
    router and refuses a communicator that sends no Origin, so every test
    through `application` fails to connect — including the ones asserting a
    refusal, which then pass for the wrong reason. Two of these did exactly
    that before this rewrite. The middleware's own half is covered separately
    by `test_a_contact_token_does_not_become_a_user_on_the_socket`.
    """
    comm = WebsocketCommunicator(
        SessionConsumer.as_asgi(), f"/ws/canopy-sessions/{session_id}/"
    )
    comm.scope["user"] = AnonymousUser()
    comm.scope["contact"] = contact
    comm.scope["url_route"] = {"kwargs": {"session_id": str(session_id)}}
    connected, _ = await comm.connect()
    return comm, connected


async def test_a_contact_can_listen_to_their_own_session():
    """The point: without this the agent's reply never reaches them."""
    from asgiref.sync import sync_to_async

    _owner, _ws, _app, priv = await sync_to_async(_world)()
    token = await sync_to_async(_contact_token)(priv)
    sid = await sync_to_async(_start_session)(token)
    contact = await sync_to_async(Contact.objects.get)()

    comm, connected = await _connect(sid, contact)

    assert connected
    snapshot = await comm.receive_json_from()
    assert snapshot["event"] == "session.state"
    await comm.disconnect()


async def test_a_contact_cannot_listen_to_someone_elses_session():
    from asgiref.sync import sync_to_async

    _owner, _ws, _app, priv = await sync_to_async(_world)()
    await sync_to_async(_contact_token)(priv, sub="u-42")  # creates the contact
    theirs_tok = await sync_to_async(_contact_token)(priv, sub="u-99")
    sid = await sync_to_async(_start_session)(theirs_tok)
    mine = await sync_to_async(lambda: Contact.objects.get(external_id="u-42"))()

    comm, connected = await _connect(sid, mine)

    assert not connected
    await comm.disconnect()


async def test_a_contact_cannot_listen_to_a_members_session():
    from asgiref.sync import sync_to_async

    owner, ws, _app, priv = await sync_to_async(_world)()
    await sync_to_async(_contact_token)(priv)
    contact = await sync_to_async(Contact.objects.get)()
    theirs = await sync_to_async(Session.objects.create)(
        workspace=ws, created_by=owner, title="private"
    )

    comm, connected = await _connect(theirs.id, contact)

    assert not connected
    await comm.disconnect()


async def test_a_contact_may_listen_but_not_act():
    """Every action is keyed on a user id, a participant role, or both.
    Refusing the whole set is honest; allowing one that happens not to
    dereference `self.user` today is how that changes silently."""
    from asgiref.sync import sync_to_async

    _owner, _ws, _app, priv = await sync_to_async(_world)()
    token = await sync_to_async(_contact_token)(priv)
    sid = await sync_to_async(_start_session)(token)
    contact = await sync_to_async(Contact.objects.get)()
    comm, connected = await _connect(sid, contact)
    assert connected
    await comm.receive_json_from()

    await comm.send_json_to({"action": "draft.update", "data": {"body": "hi"}})
    reply = await comm.receive_json_from()

    assert reply["event"] == "session.error"
    assert reply["data"]["code"] == "read_only"
    await comm.disconnect()


async def test_a_blocked_contact_resolves_to_nothing_on_the_socket():
    """Enforced in the token lookup, so it holds here without the consumer
    knowing about blocking at all — and with no scope contact, the consumer's
    existing anonymous check refuses."""
    from asgiref.sync import sync_to_async

    from apps.contacts import services
    from apps.realtime.channels_auth import _contact_from_query_token

    _owner, _ws, _app, priv = await sync_to_async(_world)()
    token = await sync_to_async(_contact_token)(priv)
    scope = {"query_string": f"token={token}".encode()}
    assert await _contact_from_query_token(scope) is not None

    await sync_to_async(services.block)(await sync_to_async(Contact.objects.get)())

    assert await _contact_from_query_token(scope) is None


async def test_an_anonymous_socket_with_no_contact_is_still_refused():
    from asgiref.sync import sync_to_async

    _owner, ws, _app, _priv = await sync_to_async(_world)()
    session = await sync_to_async(Session.objects.create)(workspace=ws, title="x")

    comm, connected = await _connect(session.id, None)

    assert not connected
    await comm.disconnect()


async def test_a_contact_token_does_not_become_a_user_on_the_socket():
    """`scope["user"]` must stay anonymous, so any consumer that has not been
    taught about contacts refuses by its existing check."""
    from asgiref.sync import sync_to_async

    from apps.realtime.channels_auth import _contact_from_query_token, _user_from_query_token

    _owner, _ws, _app, priv = await sync_to_async(_world)()
    token = await sync_to_async(_contact_token)(priv)
    scope = {"query_string": f"token={token}".encode()}

    assert await _user_from_query_token(scope) is None
    assert await _contact_from_query_token(scope) is not None
