"""Listen sessions over REST, the runner's fallback read, the control frame and the sweep.

The relay socket itself is tests/test_listen_consumer.py. Design:
docs/architecture/listen-relay.md."""
from __future__ import annotations

import base64
import datetime as dt
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.harness import runner_auth
from apps.harness.models import Runner, RunnerAccessToken, RunnerDeviceKey
from apps.listen import services
from apps.listen.models import ListenSession
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

KEY = base64.urlsafe_b64encode(b"\x04" + b"\x11" * 64).rstrip(b"=").decode()
EPH = base64.urlsafe_b64encode(b"\x04" + b"\x22" * 64).rstrip(b"=").decode()


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name="jj-mbp", *, online=True, **kw):
    ws = Workspace.objects.filter(slug="connect").first() or Workspace.objects.create(
        slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.get_or_create(workspace=ws, user=owner,
                                              defaults={"role": WorkspaceMembership.OWNER})
    return Runner.objects.create(
        name=name, kind=kw.pop("kind", Runner.EMDASH), owner=owner, workspace=ws, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now() if online else timezone.now() - dt.timedelta(minutes=5),
        capabilities={}, **kw)


@pytest.fixture
def jj():
    return _user("jj")


@pytest.fixture
def laptop(jj):
    return _runner(jj)


def _client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _meeting(runner, **hello):
    return {"runner_id": str(runner.pk), "kind": "meeting",
            "hello": {"phone_eph": EPH, "phone_key": KEY, "label": "iPhone", **hello}}


def _post(client, body):
    return client.post("/api/listen/sessions", data=body, content_type="application/json")


@pytest.fixture
def published(monkeypatch):
    sent = []
    monkeypatch.setattr(services.groups, "publish", lambda group, message: sent.append((group, message)))
    return sent


# -- runners ------------------------------------------------------------------
def test_runners_lists_only_my_own_with_online(jj, laptop):
    _runner(jj, "jj-old", online=False)
    _runner(_user("x"), "not-mine")
    res = _client(jj).get("/api/listen/runners")
    assert res.status_code == 200
    items = {r["name"]: r["online"] for r in res.json()["items"]}
    assert items == {"jj-mbp": True, "jj-old": False}


def test_runners_leaves_out_retired_runners_and_bare_computers(jj, laptop):
    gone = _runner(jj, "jj-gone")
    Runner.objects.filter(pk=gone.pk).update(status=Runner.RETIRED)
    _runner(jj, "jj-computer", kind=Runner.COMPUTER)
    names = [r["name"] for r in _client(jj).get("/api/listen/runners").json()["items"]]
    assert names == ["jj-mbp"]


# -- create ---------------------------------------------------------------------
def test_create_a_meeting_rings_the_runner(jj, laptop, django_capture_on_commit_callbacks, published):
    with django_capture_on_commit_callbacks(execute=True):
        res = _post(_client(jj), _meeting(laptop))
    assert res.status_code == 201, res.content
    sid = res.json()["id"]
    s = ListenSession.objects.get(pk=sid)
    assert (s.owner, s.runner, s.kind, s.state) == (jj, laptop, "meeting", "waiting")
    assert s.hello == {"phone_eph": EPH, "phone_key": KEY, "label": "iPhone"}
    assert published == [(f"runner.{laptop.pk.hex}", {
        "type": "runner.listen_start", "session_id": sid, "kind": "meeting", "hello": s.hello})]


def test_hello_keeps_public_keys_only(jj, laptop):
    res = _post(_client(jj), {"runner_id": str(laptop.pk), "kind": "meeting",
                              "hello": {"phone_eph": EPH, "phone_key": KEY, "label": "x", "secret": "s"}})
    assert res.status_code == 201
    assert set(ListenSession.objects.get().hello) == {"phone_eph", "phone_key", "label"}


@pytest.mark.parametrize("hello", [
    {"phone_eph": "not-a-key", "phone_key": KEY},
    {"phone_eph": EPH, "phone_key": None},  # a meeting needs the paired key
    {"phone_eph": base64.urlsafe_b64encode(b"\x02" + b"\x11" * 32).decode(), "phone_key": KEY},
])
def test_a_meeting_hello_must_carry_real_public_keys(jj, laptop, hello):
    res = _post(_client(jj), {"runner_id": str(laptop.pk), "kind": "meeting", "hello": hello})
    assert res.status_code == 422
    assert not ListenSession.objects.exists()


def test_a_pairing_has_no_phone_key(jj, laptop):
    c = _client(jj)
    ok = _post(c, {"runner_id": str(laptop.pk), "kind": "pair", "hello": {"phone_eph": EPH, "phone_key": None}})
    assert ok.status_code == 201
    bad = _post(c, {"runner_id": str(laptop.pk), "kind": "pair", "hello": {"phone_eph": EPH, "phone_key": KEY}})
    assert bad.status_code == 422


def test_a_pairing_by_code_carries_its_commitment_and_nothing_else_does(jj, laptop):
    c = _client(jj)
    commit = "A" * 43
    ok = _post(c, {"runner_id": str(laptop.pk), "kind": "pair",
                   "hello": {"phone_eph": EPH, "phone_key": None, "label": "Chrome on Mac", "commit": commit}})
    assert ok.status_code == 201
    assert ListenSession.objects.get(pk=ok.json()["id"]).hello["commit"] == commit
    for bad in ("short", "A" * 42 + "=", "A" * 42 + "/"):
        res = _post(c, {"runner_id": str(laptop.pk), "kind": "pair",
                        "hello": {"phone_eph": EPH, "phone_key": None, "commit": bad}})
        assert res.status_code == 422, bad
    assert _post(c, _meeting(laptop, commit=commit)).status_code == 422


def test_someone_elses_runner_is_404(jj):
    theirs = _runner(_user("x"), "theirs")
    assert _post(_client(jj), _meeting(theirs)).status_code == 404
    assert _post(_client(jj), _meeting(type("R", (), {"pk": uuid.uuid4()})())).status_code == 404


def test_an_offline_runner_is_409(jj):
    asleep = _runner(jj, "asleep", online=False)
    res = _post(_client(jj), _meeting(asleep))
    assert res.status_code == 409


def test_one_meeting_at_a_time_per_runner(jj, laptop):
    c = _client(jj)
    first = _post(c, _meeting(laptop))
    assert first.status_code == 201
    assert _post(c, _meeting(laptop)).status_code == 409
    # Pairing is not a meeting, and another computer is free.
    assert _post(c, {"runner_id": str(laptop.pk), "kind": "pair",
                     "hello": {"phone_eph": EPH, "phone_key": None}}).status_code == 201
    assert _post(c, _meeting(_runner(jj, "jj-mini"))).status_code == 201
    # Once it ends, the laptop takes the next.
    services.end(first.json()["id"], "test")
    assert _post(c, _meeting(laptop)).status_code == 201


def test_an_agent_login_cannot_open_one(jj, laptop):
    from apps.agents.models import Agent

    bot = _user("bot")
    ws = Workspace.objects.get(slug="connect")
    Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=jj, user=bot)
    _runner(bot, "bots-box")
    res = _post(_client(bot), _meeting(Runner.objects.get(name="bots-box")))
    assert res.status_code == 403


# -- get / end ------------------------------------------------------------------
def test_get_is_owner_only(jj, laptop):
    sid = _post(_client(jj), _meeting(laptop)).json()["id"]
    mine = _client(jj).get(f"/api/listen/sessions/{sid}")
    assert mine.status_code == 200
    assert mine.json()["state"] == "waiting" and mine.json()["kind"] == "meeting"
    assert _client(_user("x")).get(f"/api/listen/sessions/{sid}").status_code == 404
    assert _client(jj).get(f"/api/listen/sessions/{uuid.uuid4()}").status_code == 404


def test_end_closes_both_sockets_and_says_why(jj, laptop, published):
    sid = _post(_client(jj), _meeting(laptop)).json()["id"]
    assert _client(_user("x")).post(f"/api/listen/sessions/{sid}/end").status_code == 404
    res = _client(jj).post(f"/api/listen/sessions/{sid}/end")
    assert res.status_code == 200
    assert res.json()["state"] == "ended" and res.json()["end_reason"] == "ended_by_owner"
    assert (f"listen.{uuid.UUID(sid).hex}", {"type": "listen.ended", "reason": "ended_by_owner"}) in published
    # Ending again changes nothing and rings nobody.
    published.clear()
    assert _client(jj).post(f"/api/listen/sessions/{sid}/end").json()["end_reason"] == "ended_by_owner"
    assert published == []


# -- the runner's fallback read -------------------------------------------------
def _runner_token(runner) -> str:
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.AWS_IAM,
                                         role_arn=f"arn:aws:iam::1:role/{runner.name}",
                                         jkt=f"jkt-{runner.pk.hex[:8]}", status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    return raw


def test_pending_with_the_runners_own_key(jj, laptop):
    mini = _runner(jj, "jj-mini")
    sid = _post(_client(jj), _meeting(laptop)).json()["id"]
    _post(_client(jj), _meeting(mini))
    old = _post(_client(jj), {"runner_id": str(laptop.pk), "kind": "pair",
                              "hello": {"phone_eph": EPH, "phone_key": None}}).json()["id"]
    ListenSession.objects.filter(pk=old).update(created_at=timezone.now() - dt.timedelta(minutes=3))
    raw = _runner_token(laptop)
    res = Client().get("/api/runner/listen-sessions/pending", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert res.status_code == 200, res.content
    assert res.json()["items"] == [{"session_id": sid, "runner_id": str(laptop.pk), "kind": "meeting",
                                    "hello": {"phone_eph": EPH, "phone_key": KEY, "label": "iPhone"}}]
    # Its key never reads another runner's.
    other = Client().get(f"/api/runner/listen-sessions/pending?runner_id={mini.pk}",
                         HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert other.status_code == 404


def test_a_runner_key_reaches_pending_and_nothing_else_listen(laptop):
    assert runner_auth.reaches("GET", "/api/runner/listen-sessions/pending", laptop.pk)
    assert not runner_auth.reaches("POST", "/api/listen/sessions", laptop.pk)
    assert not runner_auth.reaches("GET", "/api/listen/runners", laptop.pk)
    assert runner_auth.reaches_ws(f"/ws/listen/{uuid.uuid4()}/", laptop.pk)
    assert not runner_auth.reaches_ws(f"/ws/listen/{uuid.uuid4()}/", laptop.pk, computer=True)
    assert not runner_auth.reaches_ws("/ws/presence/", laptop.pk)


def test_pending_with_a_persons_session_reads_the_named_runner(jj, laptop):
    mini = _runner(jj, "jj-mini")
    a = _post(_client(jj), _meeting(laptop)).json()["id"]
    b = _post(_client(jj), _meeting(mini)).json()["id"]
    c = _client(jj)
    assert {i["session_id"] for i in c.get("/api/runner/listen-sessions/pending").json()["items"]} == {a, b}
    named = c.get(f"/api/runner/listen-sessions/pending?runner_id={mini.pk}").json()["items"]
    assert [i["session_id"] for i in named] == [b]
    assert _client(_user("x")).get("/api/runner/listen-sessions/pending").json()["items"] == []


# -- the control frame ----------------------------------------------------------
async def test_the_control_channel_forwards_listen_start():
    from apps.realtime.consumers import RunnerConsumer

    consumer = RunnerConsumer()
    sent = []

    async def send_json(content, close=False):
        sent.append(content)

    consumer.send_json = send_json
    await consumer.runner_listen_start({"type": "runner.listen_start", "session_id": "s1", "kind": "pair",
                                        "hello": {"phone_eph": EPH, "phone_key": None, "label": ""}})
    assert sent == [{"type": "listen_start", "session_id": "s1", "kind": "pair",
                     "hello": {"phone_eph": EPH, "phone_key": None, "label": ""}}]


# -- the sweep ------------------------------------------------------------------
def _session(runner, *, state="waiting", age=dt.timedelta(), **kw):
    s = ListenSession.objects.create(owner=runner.owner, runner=runner, kind="meeting",
                                     hello={"phone_eph": EPH, "phone_key": KEY, "label": ""}, state=state, **kw)
    ListenSession.objects.filter(pk=s.pk).update(created_at=timezone.now() - age)
    return s


def test_sweep_ends_what_no_socket_will(laptop, published):
    now = timezone.now()
    fresh = _session(laptop)
    never = _session(laptop, age=dt.timedelta(minutes=3))
    left = _session(laptop, state="live", absent_since=now - dt.timedelta(seconds=61))
    back_soon = _session(laptop, state="live", absent_since=now - dt.timedelta(seconds=20))
    too_long = _session(laptop, state="live", age=dt.timedelta(hours=4, minutes=1),
                        phone_channel="a", laptop_channel="b", phone_seen_at=now, laptop_seen_at=now)
    died = _session(laptop, state="live", phone_channel="a", laptop_channel="b",
                    phone_seen_at=now - dt.timedelta(seconds=90), laptop_seen_at=now)
    assert services.sweep() == 4
    reasons = dict(ListenSession.objects.values_list("pk", "end_reason"))
    assert reasons[fresh.pk] == "" and reasons[back_soon.pk] == ""
    assert reasons[never.pk] == "not_connected"
    assert reasons[left.pk] == "peer_gone"
    assert reasons[too_long.pk] == "max_duration"
    assert reasons[died.pk] == "peer_gone"  # its socket's beat went quiet 90 s ago


def test_sweep_forgets_long_ended_rows(laptop):
    old = _session(laptop, state="ended", ended_at=timezone.now() - dt.timedelta(days=31))
    recent = _session(laptop, state="ended", ended_at=timezone.now() - dt.timedelta(days=1))
    services.sweep()
    assert list(ListenSession.objects.values_list("pk", flat=True)) == [recent.pk]
    assert not ListenSession.objects.filter(pk=old.pk).exists()


def test_the_sweep_is_registered_on_the_clock():
    from apps.common import ticks

    assert "listen.sweep" in ticks.jobs()


# -- bytes through a Redis channel layer ----------------------------------------
def test_a_redis_channel_layer_carries_frames_as_bytes():
    """Relayed frames are `bytes` in a group message; the Redis layer (msgpack) must
    round-trip them exactly, every byte value included."""
    from channels_redis.core import RedisChannelLayer

    layer = RedisChannelLayer(hosts=["redis://localhost:6379"])
    frame = bytes(range(256)) * 200
    message = {"type": "listen.frame", "b": frame}
    assert layer.deserialize(layer.serialize(message)) == message
