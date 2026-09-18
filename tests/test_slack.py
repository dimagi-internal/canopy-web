"""Slack front door (apps/slack) — driven through the real signed webhooks.

Slack's Web API is faked at the ``requests`` boundary; nothing between the
signed request and the queued Turn is mocked. That is the lesson of ace-web's
``/ace run``, which never executed anything for two months while every test
passed — because every test mocked the function that was supposed to start it.
So these assert the damage end to end: a Turn exists, on the right session,
with the right origin and actor, or it does not exist at all.
"""
from __future__ import annotations

import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.access import visible_session_q
from apps.canopy_sessions.models import Session
from apps.harness.models import Turn
from apps.slack import services
from apps.slack.models import SlackInstallation, SlackUserLink
from apps.slack.verify import sign
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user, a_workspace

pytestmark = pytest.mark.django_db

SECRET = "test-signing-secret"
TEAM, BOT, ALICE, BOB = "T1", "UBOT", "UALICE", "UBOB"


@pytest.fixture(autouse=True)
def configured(settings):
    settings.SLACK_CLIENT_ID = "123.456"
    settings.SLACK_CLIENT_SECRET = "client-secret"
    settings.SLACK_SIGNING_SECRET = SECRET
    settings.CANOPY_PUBLIC_BASE_URL = "https://canopy.test/canopy"
    return settings


class FakeSlack:
    """Records every Web API call; answers like Slack (HTTP 200, `ok` flag)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.emails = {ALICE: "alice@dimagi.com", BOB: "bob@dimagi.com"}
        self.fail: dict[str, str] = {}

    def __call__(self, url, headers=None, json=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = json if json is not None else dict(data or {})
        self.calls.append((method, payload))
        resp = mock.Mock(status_code=200)
        resp.raise_for_status = lambda: None
        if method in self.fail:
            body = {"ok": False, "error": self.fail[method]}
        elif method == "users.info":
            body = {"ok": True, "user": {"profile": {"email": self.emails.get(payload["user"], "")}}}
        elif method == "chat.postMessage":
            body = {"ok": True, "ts": "1700000999.000100"}
        elif method == "oauth.v2.access":
            body = {"ok": True, "access_token": "xoxb-new", "bot_user_id": BOT,
                    "team": {"id": TEAM, "name": "Dimagi"}}
        else:
            body = {"ok": True}
        resp.json = lambda: body
        return resp

    def said(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def slack():
    fake = FakeSlack()
    with mock.patch("apps.slack.client.requests.post", side_effect=fake):
        yield fake


@pytest.fixture
def ws(default_workspace):
    return default_workspace


@pytest.fixture
def installation(ws):
    inst = SlackInstallation(team_id=TEAM, team_name="Dimagi", bot_user_id=BOT, workspace=ws)
    inst.bot_token = "xoxb-test"
    inst.save()
    return inst


@pytest.fixture
def alice(ws):
    user = a_user("alice@dimagi.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def linked(installation, alice):
    return SlackUserLink.objects.create(installation=installation, slack_user_id=ALICE, user=alice)


@pytest.fixture
def hal(ws):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws, slack_enabled=True)


def _post(path: str, body: bytes, content_type: str, *, secret: str = SECRET, req_ts: str | None = None):
    ts = req_ts or str(int(time.time()))
    return Client().post(
        path, data=body, content_type=content_type,
        HTTP_X_SLACK_REQUEST_TIMESTAMP=ts,
        HTTP_X_SLACK_SIGNATURE=sign(secret=secret, body=body, timestamp=ts),
    )


def event(evt: dict, **kw):
    body = json.dumps({"type": "event_callback", "team_id": TEAM, "event_id": "Ev1", "event": evt}).encode()
    return _post("/api/slack/events", body, "application/json", **kw)


def mention(text: str, *, user=ALICE, ts="1700000000.000100", thread_ts=None, **kw):
    evt = {"type": "app_mention", "user": user, "text": text, "ts": ts, "channel": "C1"}
    if thread_ts:
        evt["thread_ts"] = thread_ts
    return event(evt, **kw)


def command(text: str, user=ALICE):
    from urllib.parse import urlencode
    body = urlencode({"team_id": TEAM, "channel_id": "C1", "user_id": user,
                      "command": "/canopy", "text": text}).encode()
    return _post("/api/slack/commands", body, "application/x-www-form-urlencoded")


# ---- the door --------------------------------------------------------------

def test_unconfigured_deployment_says_so(settings, slack):
    settings.SLACK_SIGNING_SECRET = "PLACEHOLDER"   # what the CFN container is born holding
    assert mention("hal hi").status_code == 503


def test_bad_signature_is_refused(slack, linked, hal):
    assert mention("hal hi", secret="wrong").status_code == 401
    assert not Turn.objects.exists()


def test_replayed_old_request_is_refused(slack, linked, hal):
    assert mention("hal hi", req_ts=str(int(time.time()) - 3600)).status_code == 401
    assert not Turn.objects.exists()


def test_url_verification_challenge(slack):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    resp = _post("/api/slack/events", body, "application/json")
    assert resp.status_code == 200 and resp.json() == {"challenge": "abc"}


# ---- a message becomes a turn ------------------------------------------------

def test_mention_queues_a_slack_turn_on_a_private_session(slack, linked, hal, alice):
    resp = mention(f"<@{BOT}> hal summarise this thread")
    assert resp.status_code == 200

    turn = Turn.objects.get()
    assert turn.origin == Turn.ORIGIN_SLACK
    assert turn.enqueued_by == alice          # the actor half of the routing key
    assert turn.prompt == "summarise this thread"
    session = turn.chat_session
    assert session.agent == hal
    assert session.created_by == alice
    assert session.metadata[services.SLACK_THREAD_KEY] == f"slack:{TEAM}:C1:1700000000.000100"

    # Only Alice can see it — a colleague in the same workspace cannot.
    bob = a_user("bob@dimagi.com")
    wsvc.ensure_member(hal.workspace, bob, WorkspaceMembership.EDITOR)
    assert not Session.objects.filter(visible_session_q(bob), pk=session.pk).exists()
    assert Session.objects.filter(visible_session_q(alice), pk=session.pk).exists()

    # She is told where it went, in her thread, visible to her alone.
    (note,) = slack.said("chat.postEphemeral")
    assert note["user"] == ALICE and note["thread_ts"] == "1700000000.000100"
    assert f"/w/{hal.workspace_id}/chat/{session.id}" in note["text"]


def test_slack_redelivery_does_not_ask_the_agent_twice(slack, linked, hal):
    mention("hal do it")
    mention("hal do it")
    assert Turn.objects.count() == 1


def test_reply_in_the_thread_continues_the_same_session(slack, linked, hal):
    mention("hal first", ts="1700000000.000100")
    mention("and a follow-up", ts="1700000050.000100", thread_ts="1700000000.000100")
    sessions = {t.chat_session_id for t in Turn.objects.all()}
    assert Turn.objects.count() == 2 and len(sessions) == 1
    assert Turn.objects.order_by("created_at").last().prompt == "and a follow-up"


def test_someone_else_in_the_thread_joins_as_a_participant(slack, linked, hal, installation, ws):
    bob = a_user("bob@dimagi.com")
    wsvc.ensure_member(ws, bob, WorkspaceMembership.EDITOR)
    SlackUserLink.objects.create(installation=installation, slack_user_id=BOB, user=bob)
    mention("hal first")
    mention("hal me too", user=BOB, ts="1700000060.000100", thread_ts="1700000000.000100")
    session = Session.objects.get()
    assert Session.objects.filter(visible_session_q(bob), pk=session.pk).exists()


def test_dm_to_the_bot_is_one_ongoing_conversation(slack, linked, hal):
    for ts in ("1700000000.000100", "1700000100.000100"):
        event({"type": "message", "channel_type": "im", "user": ALICE, "text": "hal hello",
               "ts": ts, "channel": "D1"})
    assert Turn.objects.count() == 2
    session = Session.objects.get()
    assert session.metadata[services.SLACK_THREAD_KEY] == f"slack:{TEAM}:D1:dm"


def test_bots_and_edits_are_ignored(slack, linked, hal):
    event({"type": "message", "channel_type": "im", "bot_id": "B1", "user": ALICE,
           "text": "hal hi", "ts": "1", "channel": "D1"})
    event({"type": "message", "channel_type": "im", "subtype": "message_changed", "user": ALICE,
           "text": "hal hi", "ts": "2", "channel": "D1"})
    # A plain channel message is not something we subscribe to — never ingest it.
    event({"type": "message", "channel_type": "channel", "user": ALICE, "text": "hal hi",
           "ts": "3", "channel": "C1"})
    assert not Turn.objects.exists()
    assert not slack.calls


# ---- and when it must not ----------------------------------------------------

def test_unlinked_user_gets_a_link_and_nothing_is_queued(slack, installation, hal):
    mention("hal do it")
    assert not Turn.objects.exists()
    (note,) = slack.said("chat.postEphemeral")
    assert "/auth/slack/link/?token=" in note["text"]


def test_linked_but_not_a_member_is_refused(slack, installation, hal, ws):
    outsider = a_user("outsider@dimagi.com")
    other = a_workspace("elsewhere")
    wsvc.ensure_member(other, outsider, WorkspaceMembership.OWNER)
    SlackUserLink.objects.create(installation=installation, slack_user_id=ALICE, user=outsider)
    mention("hal do it")
    assert not Turn.objects.exists()
    assert "isn't a member" in slack.said("chat.postEphemeral")[0]["text"]


def test_agent_not_turned_on_for_slack_is_unreachable(slack, linked, ws):
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, slack_enabled=False)
    Agent.objects.create(slug="eva", name="Eva", workspace=ws, slack_enabled=True)
    Agent.objects.create(slug="ada", name="Ada", workspace=ws, slack_enabled=True)
    mention("hal do it")
    assert not Turn.objects.exists()
    text = slack.said("chat.postEphemeral")[0]["text"]
    assert "`ada`" in text and "`eva`" in text and "`hal`" not in text


def test_agent_in_another_workspace_is_unreachable(slack, linked):
    other = a_workspace("elsewhere")
    Agent.objects.create(slug="hal", name="Hal", workspace=other, slack_enabled=True)
    mention("hal do it")
    assert not Turn.objects.exists()


def test_only_enabled_agent_is_the_default(slack, linked, hal):
    mention(f"<@{BOT}> what's on today?")
    assert Turn.objects.get().prompt == "what's on today?"


# ---- /canopy ----------------------------------------------------------------

def test_slash_command_anchors_a_thread_and_queues(slack, linked, hal):
    resp = command("hal draft the update")
    assert resp.status_code == 200 and resp.json()["response_type"] == "ephemeral"
    (anchor,) = slack.said("chat.postMessage")
    assert anchor["channel"] == "C1" and "hal" in anchor["text"]
    turn = Turn.objects.get()
    assert turn.origin == Turn.ORIGIN_SLACK and turn.prompt == "draft the update"
    assert turn.chat_session.metadata["slack_thread_ts"] == "1700000999.000100"


def test_slash_command_when_bot_is_not_in_the_channel(slack, linked, hal):
    slack.fail["chat.postMessage"] = "not_in_channel"
    resp = command("hal draft the update")
    assert "/invite" in resp.json()["text"]
    assert not Turn.objects.exists()


def test_slash_command_from_unlinked_user_posts_nothing(slack, installation, hal):
    resp = command("hal draft the update")
    assert "/auth/slack/link/" in resp.json()["text"]
    assert not slack.said("chat.postMessage") and not Turn.objects.exists()


# ---- linking an account --------------------------------------------------------

def _link_client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _token_from(url: str) -> str:
    return parse_qs(urlparse(url).query)["token"][0]


def test_link_joins_accounts_whose_emails_match(slack, installation, alice):
    token = _token_from(services.link_url(TEAM, ALICE))
    resp = _link_client(alice).get("/auth/slack/link/", {"token": token})
    assert resp.status_code == 200
    assert SlackUserLink.objects.get(slack_user_id=ALICE).user == alice


def test_link_refuses_a_forwarded_link(slack, installation, alice):
    """The phishing case: Bob's Slack link, opened by Alice, must not bind them."""
    token = _token_from(services.link_url(TEAM, BOB))
    resp = _link_client(alice).get("/auth/slack/link/", {"token": token})
    assert resp.status_code == 403
    assert not SlackUserLink.objects.exists()


def test_link_rejects_a_tampered_token(slack, installation, alice):
    resp = _link_client(alice).get("/auth/slack/link/", {"token": "not-a-token"})
    assert resp.status_code == 400 and not SlackUserLink.objects.exists()


# ---- installing -----------------------------------------------------------------

def _owner(ws):
    user = a_user("owner@dimagi.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.OWNER)
    return user


def test_only_an_owner_may_install(slack, ws, alice):
    resp = _link_client(alice).get("/auth/slack/install/", {"workspace": ws.slug})
    assert resp.status_code == 403


def test_install_round_trip_stores_an_encrypted_token(slack, ws):
    c = _link_client(_owner(ws))
    start = c.get("/auth/slack/install/", {"workspace": ws.slug})
    assert start.status_code == 302
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    resp = c.get("/auth/slack/callback/", {"code": "abc", "state": state})
    assert resp.status_code == 200
    inst = SlackInstallation.objects.get(team_id=TEAM)
    assert inst.workspace == ws and inst.bot_user_id == BOT
    assert inst.bot_token == "xoxb-new" and "xoxb-new" not in inst.bot_token_enc


def test_install_callback_rejects_a_foreign_state(slack, ws):
    c = _link_client(_owner(ws))
    c.get("/auth/slack/install/", {"workspace": ws.slug})
    resp = c.get("/auth/slack/callback/", {"code": "abc", "state": "forged"})
    assert resp.status_code == 400 and not SlackInstallation.objects.exists()


def test_install_cannot_steal_a_team_bound_to_a_workspace_you_do_not_own(slack, installation):
    other = a_workspace("elsewhere")
    c = _link_client(_owner(other))
    start = c.get("/auth/slack/install/", {"workspace": other.slug})
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    resp = c.get("/auth/slack/callback/", {"code": "abc", "state": state})
    assert resp.status_code == 409
    installation.refresh_from_db()
    assert installation.workspace_id != other.slug


# ---- the owner's switch -----------------------------------------------------------

def test_only_an_owner_turns_slack_on(ws, alice):
    Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    denied = _link_client(alice).patch("/api/agents/hal/slack", {"slack_enabled": True},
                                       content_type="application/json")
    assert denied.status_code == 403
    ok = _link_client(_owner(ws)).patch("/api/agents/hal/slack", {"slack_enabled": True},
                                        content_type="application/json")
    assert ok.status_code == 200 and ok.json()["slack_enabled"] is True
    assert Agent.objects.get(slug="hal").slack_enabled is True
