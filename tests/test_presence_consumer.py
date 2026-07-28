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
        "page_key": "canopy:~global:settings",
        "sub_location": "Settings",
    })
    message = await communicator.receive_json_from(timeout=2)
    assert message["event"] == "presence.roster"
    assert message["data"]["page_key"] == "canopy:~global:settings"
    assert [v["email"] for v in message["data"]["viewers"]] == [user.email]
    await communicator.disconnect()


# -- Final-review security regressions --


@pytest.mark.asyncio
async def test_the_bare_word_global_no_longer_skips_the_membership_gate(member_user):
    """FINDING 1. `global` used to be the sentinel that bypassed the gate,
    and workspace slugs carry NO reserved-name validation — so any user
    could create a workspace called `global` and every OTHER authenticated
    user could then read (and write themselves into) its roster by asserting
    `canopy:global:<resource>`. The sentinel moved to `~global`; `global` is
    now an ordinary tenant name, gated like any other."""
    from apps.realtime import presence_store

    communicator, connected = await _connect(member_user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:global:activity",
        "sub_location": "Activity",
    })
    assert await communicator.receive_nothing(timeout=1)
    assert await presence_store.roster("canopy:global:activity") == []
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_workspace_shadowing_the_sentinel_cannot_be_used_to_bypass_the_gate(
    member_user,
):
    """FINDING 1, belt and braces. WORKSPACE_RE cannot match `~global`, and
    `Workspace.slug` now rejects that charset on every save path — but this
    check stays: it is what makes the guard survive a row that predates the
    charset rule, arrives by raw SQL, or is restored from an old dump. If such
    a row exists, the global branch must not run: otherwise that row's roster
    is the one surface every authenticated user reaches without a membership
    check.

    Seeded via `bulk_create`, which does not call `Model.save()` — the only
    way to mint the hostile row now that the model validates its own slug.
    """
    from apps.realtime import presence_store
    from apps.workspaces.models import Workspace

    await database_sync_to_async(Workspace.objects.bulk_create)(
        [Workspace(slug="~global", display_name="Shadow", created_by=member_user)]
    )

    communicator, connected = await _connect(member_user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:~global:settings",
        "sub_location": "Settings",
    })
    assert await communicator.receive_nothing(timeout=1)
    assert await presence_store.roster("canopy:~global:settings") == []
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_page_key_for_a_different_app_is_rejected(member_user):
    """The app segment namespaces rosters across sibling deployments. It was
    parsed and then ignored, so a client could park canopy viewers on
    `ace:<ws>:<resource>` — a key ace-web's own users legitimately occupy."""
    from apps.realtime import presence_store

    communicator, connected = await _connect(member_user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "ace:test-ws:activity",
        "sub_location": "Activity",
    })
    assert await communicator.receive_nothing(timeout=1)
    assert await presence_store.roster("ace:test-ws:activity") == []
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_colon_bearing_workspace_slug_gets_no_presence_rather_than_a_foreign_gate():
    """FINDING 2. A workspace slugged `acme:eu` renders `canopy:acme:eu:...`,
    which the bounded split reads as workspace `acme`. Its own members must
    NOT be admitted on the strength of that (they are not members of
    `acme`), and — the leak direction — a member of an unrelated `acme` must
    never end up in a roster alongside them.

    A colon slug is now unmintable at the tenancy layer (see
    `apps/workspaces/models.py::SLUG_PATTERN` — that is the real fix, because
    the ambiguity is irreducible once such a slug exists). This test keeps the
    presence-side guard honest against a row that predates the rule or lands
    by raw SQL, so it seeds via `bulk_create`, which bypasses `Model.save()`.
    """
    from apps.realtime import presence_store
    from apps.workspaces.models import Workspace, WorkspaceMembership

    @sync_to_async
    def _seed():
        eu_user = get_user_model().objects.create_user(username="eu@x.com", email="eu@x.com")
        eu_ws = Workspace.objects.bulk_create(
            [Workspace(slug="acme:eu", display_name="Acme EU", created_by=eu_user)]
        )[0]
        WorkspaceMembership.objects.create(workspace=eu_ws, user=eu_user, role="editor")
        return eu_user

    eu_user = await _seed()

    communicator, connected = await _connect(eu_user)
    assert connected
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:acme:eu:activity",
        "sub_location": "Activity",
    })
    assert await communicator.receive_nothing(timeout=1)
    assert await presence_store.roster("canopy:acme:eu:activity") == []
    await communicator.disconnect()


@pytest.mark.asyncio
async def test_opting_out_on_the_page_you_are_already_on_removes_you_immediately(member_user):
    """A re-enter on the SAME key never passes through _leave_current, so
    without an explicit forget on the invisible branch the previously-written
    field survives its 60s TTL — and the broadcast that follows re-serves a
    roster still containing the user who just opted out."""
    from apps.realtime import presence_store

    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    msg = await communicator.receive_json_from(timeout=2)
    assert [v["email"] for v in msg["data"]["viewers"]] == [member_user.email]

    await _acreate_pref(member_user)  # opt out, same page, no navigation

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    msg = await communicator.receive_json_from(timeout=2)
    assert msg["data"]["viewers"] == []
    assert await presence_store.roster("canopy:test-ws:activity") == []

    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_redis_failure_degrades_to_an_empty_roster_instead_of_killing_the_socket(
    member_user, monkeypatch
):
    """Presence is an enhancement. An unwrapped store call raises straight out
    of receive_json and Channels tears the consumer down, taking the page's
    socket with it."""

    async def _boom(*args, **kwargs):
        raise RuntimeError("redis is having a moment")

    monkeypatch.setattr("apps.realtime.presence_store.touch", _boom)
    monkeypatch.setattr("apps.realtime.presence_store.roster", _boom)
    monkeypatch.setattr("apps.realtime.presence_store.forget", _boom)

    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "Activity",
    })
    msg = await communicator.receive_json_from(timeout=2)
    assert msg["event"] == "presence.roster"
    assert msg["data"]["viewers"] == []

    # ...and the socket is still alive and still serving frames afterwards.
    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    msg = await communicator.receive_json_from(timeout=2)
    assert msg["data"]["page_key"] == "canopy:test-ws:opp:b"

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


# -- Fix-round-2 regressions: each of these must fail if its corresponding
# fix is reverted. See task-7-report.md's "Fix round 2" section for the
# mutant-by-mutant verification that they actually do. --


@pytest.mark.asyncio
async def test_visibility_is_re_read_on_every_enter_not_cached_at_connect(member_user):
    """Regression for a connect-time `self.visible` snapshot: opting out
    mid-connection must stop future writes on the very next enter, not just
    at the next reconnect."""
    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a",
        "sub_location": "Page A",
    })
    msg_a = await communicator.receive_json_from(timeout=2)
    assert [v["email"] for v in msg_a["data"]["viewers"]] == [member_user.email]

    await _acreate_pref(member_user)  # opt out mid-connection, no reconnect

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    msg_b = await communicator.receive_json_from(timeout=2)
    assert msg_b["data"]["viewers"] == []

    await communicator.disconnect()


@pytest.mark.asyncio
async def test_membership_is_re_checked_on_every_enter_not_cached_at_connect(member_user):
    """Regression for a connect-time `self.workspaces` snapshot: a
    membership revoked mid-connection must reject the very next enter into
    that workspace, not just the next reconnect."""
    from apps.workspaces.models import WorkspaceMembership

    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a",
        "sub_location": "Page A",
    })
    await communicator.receive_json_from(timeout=2)  # still a member here

    await database_sync_to_async(
        WorkspaceMembership.objects.filter(workspace_id="test-ws", user=member_user).delete
    )()

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    assert await communicator.receive_nothing(timeout=1)

    from apps.realtime import presence_store

    assert await presence_store.roster("canopy:test-ws:opp:b") == []

    await communicator.disconnect()


@pytest.mark.asyncio
async def test_a_revoked_member_is_torn_down_from_their_old_page_too(member_user):
    """Regression for the teardown-on-reject fix: a rejected enter (here,
    lost membership) must leave the connection's PREVIOUS group as well —
    otherwise a revoked member keeps receiving that workspace's roster
    broadcasts until they disconnect or the Redis TTL expires."""
    from apps.workspaces.models import WorkspaceMembership

    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a",
        "sub_location": "Page A",
    })
    await communicator.receive_json_from(timeout=2)

    await database_sync_to_async(
        WorkspaceMembership.objects.filter(workspace_id="test-ws", user=member_user).delete
    )()

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    assert await communicator.receive_nothing(timeout=1)

    from apps.realtime import presence_store

    assert await presence_store.roster("canopy:test-ws:opp:a") == []

    await communicator.disconnect()


@pytest.mark.asyncio
async def test_whitespace_variant_keys_land_in_the_same_roster(member_user, second_member_user):
    """Regression for using the raw client string instead of the canonical
    (stripped) key: two connections entering the "same" page with different
    whitespace must join the SAME group/Redis key, not silently fragment
    the roster across two."""
    comm1, connected1 = await _connect(member_user)
    comm2, connected2 = await _connect(second_member_user)
    assert connected1
    assert connected2

    await comm1.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy: test-ws :activity",
        "sub_location": "",
    })
    msg1 = await comm1.receive_json_from(timeout=2)
    assert msg1["data"]["page_key"] == "canopy:test-ws:activity"

    await comm2.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:activity",
        "sub_location": "",
    })
    # If the two connections joined different groups (raw string used as the
    # key), comm1 never gets notified of comm2's entry and this times out.
    msg1_updated = await comm1.receive_json_from(timeout=2)
    msg2 = await comm2.receive_json_from(timeout=2)

    expected = {member_user.email, second_member_user.email}
    assert {v["email"] for v in msg1_updated["data"]["viewers"]} == expected
    assert {v["email"] for v in msg2["data"]["viewers"]} == expected

    await comm1.disconnect()
    await comm2.disconnect()


@pytest.mark.asyncio
async def test_idle_state_does_not_leak_across_navigation(member_user):
    """Regression for not resetting self.idle / self._last_broadcast_idle on
    a page-key change: going idle on page A must not show as idle the
    instant the user arrives on page B."""
    communicator, connected = await _connect(member_user)
    assert connected

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:a",
        "sub_location": "Page A",
    })
    await communicator.receive_json_from(timeout=2)

    await communicator.send_json_to({"type": "presence.heartbeat", "idle": True})
    idle_msg = await communicator.receive_json_from(timeout=2)
    assert idle_msg["data"]["viewers"][0]["idle"] is True

    await communicator.send_json_to({
        "type": "presence.enter",
        "page_key": "canopy:test-ws:opp:b",
        "sub_location": "Page B",
    })
    msg_b = await communicator.receive_json_from(timeout=2)
    assert msg_b["data"]["viewers"][0]["idle"] is False

    await communicator.disconnect()
