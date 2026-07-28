import fakeredis.aioredis
import pytest
from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model

from apps.realtime.presence_consumer import PresenceConsumer

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _get_redis():
        return client

    monkeypatch.setattr("apps.common.redis_client.get_redis", _get_redis)
    return client


@pytest.fixture
def member_user(db):
    """A user who is a member of the workspace slug 'test-ws'.

    NOTE: canopy-web's Workspace has no `members` M2M — membership is the
    WorkspaceMembership through model, `slug` is the primary key (not a
    separate `pk=`/`name=` pair), and `created_by` is a required FK. See
    `apps.workspaces.services.user_workspace_slugs`, which this fixture must
    satisfy: it reads `WorkspaceMembership.objects.filter(user=user)`.
    """
    from apps.workspaces.models import Workspace, WorkspaceMembership

    user = get_user_model().objects.create_user(username="m@x.com", email="m@x.com")
    workspace = Workspace.objects.create(
        slug="test-ws", display_name="Test WS", created_by=user
    )
    WorkspaceMembership.objects.create(workspace=workspace, user=user, role="editor")
    return user


@sync_to_async
def _acreate_pref(user):
    from apps.realtime.models import PresencePreference

    PresencePreference.objects.create(user=user, show_presence=False)


async def _connect(user):
    communicator = WebsocketCommunicator(PresenceConsumer.as_asgi(), "/ws/presence/")
    communicator.scope["user"] = user
    connected, _ = await communicator.connect()
    return communicator, connected


@pytest.mark.asyncio
async def test_anonymous_is_rejected():
    from django.contrib.auth.models import AnonymousUser

    communicator, connected = await _connect(AnonymousUser())
    assert connected is False
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_entering_a_page_broadcasts_a_roster_containing_you(member_user):
    communicator, connected = await _connect(member_user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a/run-001",
        "sub_location": "run overview",
    })
    message = await communicator.receive_json_from(timeout=2)
    assert message["event"] == "presence.roster"
    assert message["data"]["page_key"] == "canopy:test-ws:opp:a/run-001"
    assert [v["email"] for v in message["data"]["viewers"]] == [member_user.email]
    assert message["data"]["viewers"][0]["self"] is True
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_foreign_workspace_key_is_silently_rejected(member_user):
    """Membership is checked server-side — the page key is client-supplied."""
    communicator, _ = await _connect(member_user)
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:someone-elses-workspace:opp:secret/run-001",
        "sub_location": "",
    })
    assert await communicator.receive_nothing(timeout=1)
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_malformed_page_key_is_silently_rejected(member_user):
    communicator, _ = await _connect(member_user)
    await communicator.send_json_to({"type": "presence.enter", "page_key": "junk", "sub_location": ""})
    assert await communicator.receive_nothing(timeout=1)
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_an_opted_out_user_receives_rosters_but_is_absent_from_them(member_user):
    from apps.realtime.models import PresencePreference

    await _acreate_pref(member_user)
    communicator, _ = await _connect(member_user)
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    message = await communicator.receive_json_from(timeout=2)
    assert message["event"] == "presence.roster"
    assert message["data"]["viewers"] == []
    await communicator.disconnect()
    # This repo's convention (see test_chat_session_consumer.py) wraps sync
    # ORM calls in database_sync_to_async when made from inside an async
    # test — a running event loop trips Django's SynchronousOnlyOperation
    # guard on a bare `.exists()` call here.
    exists = await database_sync_to_async(
        PresencePreference.objects.filter(user=member_user).exists
    )()
    assert exists


@pytest.mark.asyncio
async def test_disconnect_removes_the_viewer(member_user, fake_redis):
    communicator, _ = await _connect(member_user)
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    await communicator.receive_json_from(timeout=2)
    await communicator.disconnect()

    from apps.realtime import presence_store

    assert await presence_store.roster("canopy:test-ws:activity") == []
