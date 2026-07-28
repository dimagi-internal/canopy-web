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


@pytest.fixture
def second_member_user(member_user):
    """A second member of the SAME 'test-ws' workspace, for multi-viewer tests."""
    from apps.workspaces.models import Workspace, WorkspaceMembership

    workspace = Workspace.objects.get(slug="test-ws")
    user = get_user_model().objects.create_user(username="n@x.com", email="n@x.com")
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

    communicator = WebsocketCommunicator(PresenceConsumer.as_asgi(), "/ws/presence/")
    communicator.scope["user"] = AnonymousUser()
    connected, code = await communicator.connect()
    assert connected is False
    assert code == 4001
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
    """Membership is checked server-side — the page key is client-supplied.

    Silence alone would also pass an implementation that joined the group
    without ever writing/broadcasting — assert the group was never joined
    at all by checking nothing landed in Redis for that key either.
    """
    from apps.realtime import presence_store

    communicator, _ = await _connect(member_user)
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:someone-elses-workspace:opp:secret/run-001",
        "sub_location": "",
    })
    assert await communicator.receive_nothing(timeout=1)
    assert await presence_store.roster("canopy:someone-elses-workspace:opp:secret/run-001") == []
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


@pytest.mark.asyncio
async def test_two_viewers_on_the_same_page_see_each_other_with_correct_self_flag(
    member_user, second_member_user
):
    comm1, connected1 = await _connect(member_user)
    comm2, connected2 = await _connect(second_member_user)
    assert connected1
    assert connected2

    await comm1.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    await comm1.receive_json_from(timeout=2)  # solo roster, not under test here

    await comm2.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    # comm2 joining broadcasts a fresh roster to the whole group — both
    # sockets get a message, each with its OWN "self" flag.
    msg1 = await comm1.receive_json_from(timeout=2)
    msg2 = await comm2.receive_json_from(timeout=2)

    for msg, viewing_as in [(msg1, member_user), (msg2, second_member_user)]:
        viewers = {v["email"]: v for v in msg["data"]["viewers"]}
        assert set(viewers) == {member_user.email, second_member_user.email}
        assert viewers[viewing_as.email]["self"] is True
        other = second_member_user if viewing_as is member_user else member_user
        assert viewers[other.email]["self"] is False

    await comm1.disconnect()
    await comm2.disconnect()


@pytest.mark.asyncio
async def test_opted_out_viewer_sees_others_but_others_do_not_see_them(
    member_user, second_member_user
):
    await _acreate_pref(member_user)  # member_user opts out; second_member_user stays visible

    comm1, _ = await _connect(member_user)
    comm2, _ = await _connect(second_member_user)

    await comm1.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "",
    })
    msg1_solo = await comm1.receive_json_from(timeout=2)
    assert msg1_solo["data"]["viewers"] == []  # opted-out, never written

    await comm2.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "",
    })
    msg1 = await comm1.receive_json_from(timeout=2)
    msg2 = await comm2.receive_json_from(timeout=2)

    # The opted-out viewer RECEIVES a roster containing the visible viewer...
    assert [v["email"] for v in msg1["data"]["viewers"]] == [second_member_user.email]
    # ...but the visible viewer's roster does NOT contain the opted-out one.
    assert [v["email"] for v in msg2["data"]["viewers"]] == [second_member_user.email]

    await comm1.disconnect()
    await comm2.disconnect()


@pytest.mark.asyncio
async def test_a_global_page_key_succeeds_with_no_workspace_memberships():
    user = await database_sync_to_async(get_user_model().objects.create_user)(
        username="g@x.com", email="g@x.com"
    )
    communicator, connected = await _connect(user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:global:settings",
        "sub_location": "Settings",
    })
    message = await communicator.receive_json_from(timeout=2)
    assert message["event"] == "presence.roster"
    assert message["data"]["page_key"] == "canopy:global:settings"
    assert [v["email"] for v in message["data"]["viewers"]] == [user.email]
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_navigating_to_a_new_page_removes_you_from_the_old_rosters(member_user):
    from apps.realtime import presence_store

    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a",
        "sub_location": "Page A",
    })
    await communicator.receive_json_from(timeout=2)  # roster for page A

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    message = await communicator.receive_json_from(timeout=2)
    assert message["data"]["page_key"] == "canopy:test-ws:opp:b"
    assert [v["email"] for v in message["data"]["viewers"]] == [member_user.email]

    assert await presence_store.roster("canopy:test-ws:opp:a") == []
    assert len(await presence_store.roster("canopy:test-ws:opp:b")) == 1

    await communicator.disconnect()
