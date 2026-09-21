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
import json as _json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.access import visible_session_q
from apps.canopy_sessions.models import Session
from apps.contacts import services as contacts_services
from apps.contacts.models import Contact
from apps.events.models import Event
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
        self.users = {
            ALICE: {"name": "alice", "profile": {"email": "alice@dimagi.com", "real_name": "Alice A"}},
            BOB: {"name": "bob", "profile": {"email": "bob@dimagi.com", "real_name": "Bob B"}},
        }
        self.installer = ALICE
        self.fail: dict[str, str] = {}
        # The Slack APP's own config, as apps.manifest.export returns it.
        self.manifest = {
            "display_information": {"name": "Canopy"},
            "features": {"slash_commands": [
                {"command": "/canopy", "url": "https://canopy.test/canopy/api/slack/commands",
                 "description": "Ask a canopy agent"},
                {"command": "/standup", "url": "https://elsewhere.example/standup",
                 "description": "Not canopy's"},
            ]},
            "settings": {"event_subscriptions": {"bot_events": ["app_mention", "message.im"]}},
        }
        self.rotations = 0
        # email -> Slack user id, for users.lookupByEmail.
        self.lookup: dict[str, str] = {}

    def __call__(self, url, headers=None, json=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = json if json is not None else dict(data or {})
        self.calls.append((method, payload))
        resp = mock.Mock(status_code=200)
        resp.raise_for_status = lambda: None
        if method in self.fail:
            body = {"ok": False, "error": self.fail[method]}
        elif method == "users.info":
            body = {"ok": True, "user": self.users.get(payload["user"], {"profile": {}})}
        elif method == "agents.sessions.setStatus":
            body = {"ok": True}
        elif method == "chat.postMessage":
            body = {"ok": True, "ts": "1700000999.000100", "channel": payload.get("channel")}
        elif method == "users.lookupByEmail":
            uid = self.lookup.get(payload.get("email", ""))
            body = {"ok": True, "user": {"id": uid}} if uid else {"ok": False, "error": "users_not_found"}
        elif method == "oauth.v2.access":
            body = {"ok": True, "access_token": "xoxb-new", "bot_user_id": BOT, "app_id": "A_CANOPY",
                    "team": {"id": TEAM, "name": "Dimagi"}, "authed_user": {"id": self.installer}}
        elif method == "tooling.tokens.rotate":
            self.rotations += 1
            n = self.rotations
            body = {"ok": True, "token": f"xoxe.xoxp-access-{n}", "refresh_token": f"xoxe-refresh-{n}",
                    "iat": 1000, "exp": 1000 + 43200}
        elif method == "apps.manifest.export":
            body = {"ok": True, "manifest": _json.loads(_json.dumps(self.manifest))}
        elif method == "apps.manifest.update":
            self.manifest = _json.loads(payload["manifest"])
            body = {"ok": True, "app_id": payload["app_id"], "permissions_updated": False}
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

    # She is told at once, in the thread, where it stands — here, that nothing
    # can run it — with the canopy link. Public and threaded: a status line,
    # not a private note that reads as silence (the first live mention on labs).
    assert not slack.said("chat.postEphemeral")
    (line,) = slack.said("chat.postMessage")
    assert line["thread_ts"] == "1700000000.000100"
    assert "no runner is set up to run `hal`" in line["text"]
    assert f"/w/{hal.workspace_id}/chat/{session.id}" in line["text"]


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

def test_reply_inside_a_thread_stays_in_that_thread(slack, linked, hal):
    mention("hal more", ts="1700000050.000100", thread_ts="1700000000.000100")
    assert slack.said("chat.postMessage")[0]["thread_ts"] == "1700000000.000100"


def test_member_matched_by_email_is_linked_without_a_click(slack, installation, hal, alice):
    mention("hal hello")
    assert SlackUserLink.objects.get(slack_user_id=ALICE).user == alice
    turn = Turn.objects.get()
    assert turn.enqueued_by == alice
    assert (turn.initiator_kind, turn.initiator_assurance) == ("user", "slack_email")
    assert turn.chat_session.created_by == alice and turn.chat_session.contact is None


def _is_contact_turn(turn) -> Contact:
    session = turn.chat_session
    assert session.created_by is None and session.contact is not None
    assert turn.enqueued_by is None
    assert (turn.initiator_kind, turn.initiator_contact) == ("contact", session.contact)
    return session.contact


def test_someone_with_no_canopy_account_is_answered_as_a_contact(slack, installation, hal, ws):
    mention("hal who owns the budget?")
    contact = _is_contact_turn(Turn.objects.get())
    assert contact.workspace == ws and contact.source == Contact.SOURCE_SLACK
    assert contact.external_id == f"{TEAM}:{ALICE}"
    assert contact.auth_result == Contact.AUTH_SLACK
    assert (contact.email, contact.display_name) == ("alice@dimagi.com", "Alice A")
    # Grants nothing: not a member, and no member can open the conversation.
    assert not WorkspaceMembership.objects.filter(workspace=ws, user__email="alice@dimagi.com").exists()
    owner = _owner(ws)
    assert not Session.objects.filter(visible_session_q(owner)).exists()
    # No canopy link in the status line — a contact could not open it.
    assert "/w/" not in slack.said("chat.postMessage")[0]["text"]


def test_the_same_slack_user_is_one_contact(slack, installation, hal):
    mention("hal one", ts="1700000000.000100")
    mention("hal two", ts="1700000100.000100")
    assert Contact.objects.count() == 1
    assert Contact.objects.get().message_count == 2


def test_a_guest_is_a_contact_even_with_a_member_email(slack, installation, hal, alice):
    slack.users[ALICE]["is_restricted"] = True
    mention("hal hello")
    _is_contact_turn(Turn.objects.get())
    assert not SlackUserLink.objects.exists()


def test_an_ambiguous_email_is_not_linked(slack, installation, hal, alice):
    a_user("ALICE@dimagi.com")          # a second account, same address
    mention("hal hello")
    _is_contact_turn(Turn.objects.get())
    assert not SlackUserLink.objects.exists()


def test_a_linked_user_outside_the_workspace_is_a_contact(slack, installation, hal):
    outsider = a_user("outsider@dimagi.com")
    wsvc.ensure_member(a_workspace("elsewhere"), outsider, WorkspaceMembership.OWNER)
    SlackUserLink.objects.create(installation=installation, slack_user_id=ALICE, user=outsider)
    mention("hal do it")
    _is_contact_turn(Turn.objects.get())
    assert not wsvc.is_member(outsider, installation.workspace_id)


def test_a_blocked_contact_is_refused_and_logged(slack, installation, hal, ws):
    mention("hal first", ts="1700000000.000100")
    contacts_services.block(Contact.objects.get(), reason="spam")
    mention("hal again", ts="1700000100.000100")
    assert Turn.objects.count() == 1
    assert Event.objects.filter(workspace=ws, source="slack", kind="slack.blocked").exists()


def test_a_member_joining_a_contacts_thread_can_then_see_it(slack, installation, hal, alice):
    slack.users[BOB] = {"profile": {"email": "stranger@partner.org"}}
    mention("hal hi", user=BOB)
    mention("hal me too", ts="1700000060.000100", thread_ts="1700000000.000100")
    session = Session.objects.get()
    assert session.contact is not None
    assert Session.objects.filter(visible_session_q(alice), pk=session.pk).exists()


def test_a_refusal_leaves_a_row_in_the_event_log(slack, linked, ws):
    Agent.objects.create(slug="eva", name="Eva", workspace=ws, slack_enabled=True)
    Agent.objects.create(slug="ada", name="Ada", workspace=ws, slack_enabled=True)
    mention("no agent named here")
    event = Event.objects.get(source="slack")
    assert event.kind == "slack.no_agent" and event.payload["user"] == ALICE


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
    # ONE message: the anchor IS the status line, edited in place. Two would say
    # half the story each ("you asked hal…" / "picking this up on…").
    (anchor,) = slack.said("chat.postMessage")
    assert anchor["channel"] == "C1" and "asked *hal*: draft the update" in anchor["text"]
    (edit,) = slack.said("chat.update")
    assert edit["ts"] == "1700000999.000100"
    # One message, both halves: what was asked, and what is happening to it.
    assert "asked *hal*: draft the update" in edit["text"] and "Queued" in edit["text"]
    turn = Turn.objects.get()
    assert turn.origin == Turn.ORIGIN_SLACK and turn.prompt == "draft the update"
    assert turn.chat_session.metadata["slack_thread_ts"] == "1700000999.000100"


def test_a_slash_command_line_keeps_the_ask_on_every_later_edit(slack, linked, hal, alice):
    """The turn moves on; the message must not lose what was asked."""
    from django.utils import timezone as _tz

    from apps.harness.models import Runner as _Runner
    from apps.slack import status

    command("hal draft the update")
    turn = Turn.objects.get()
    turn.claimed_by = _Runner.objects.create(name="jj-mbp", kind=_Runner.EMDASH, host="jj-mac",
                                             paired_by=alice, workspace=hal.workspace,
                                             status=_Runner.ONLINE, last_heartbeat_at=_tz.now())
    turn.status = Turn.RUNNING
    turn.save(update_fields=["status", "claimed_by"])
    status.refresh(turn)
    assert "asked *hal*: draft the update" in slack.said("chat.update")[-1]["text"]


def test_slash_command_when_bot_is_not_in_the_channel(slack, linked, hal):
    slack.fail["chat.postMessage"] = "not_in_channel"
    resp = command("hal draft the update")
    assert "/invite" in resp.json()["text"]
    assert not Turn.objects.exists()


def test_slash_command_from_a_contact_is_answered(slack, installation, hal):
    resp = command("hal draft the update")
    assert resp.json()["text"] == "Sent to `hal` — the reply will come back here."
    _is_contact_turn(Turn.objects.get())


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
    # The installer proved both identities in that one trip, so they are linked.
    assert SlackUserLink.objects.get(slack_user_id=ALICE).user.email == "owner@dimagi.com"
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


# ---- replies come back to the thread (apps/slack/relay.py) -------------------------
#
# Driven through `append_events`, the call the runner's POST lands on, with
# on_commit callbacks executed — the signal is post-commit, so a test that did
# not run them would pass with the relay disconnected.

from apps.harness import services as harness_services  # noqa: E402
from apps.slack.models import SlackRelayPost  # noqa: E402


def _reply(turn, *events, capture):
    with capture(execute=True):
        harness_services.append_events(turn, list(events))


def test_the_agents_reply_is_posted_into_the_thread(slack, linked, hal, django_capture_on_commit_callbacks):
    mention("hal summarise")
    turn = Turn.objects.get()
    slack.calls.clear()                            # the status line; replies are what is under test
    _reply(turn,
           {"kind": "status", "payload": {"status": "running"}},
           {"kind": "tool_start", "payload": {"text": "Bash"}},
           {"kind": "assistant", "payload": {"text": "**Done.** See [the doc](https://example.com/d)."}},
           capture=django_capture_on_commit_callbacks)
    (post,) = slack.said("chat.postMessage")
    assert post["channel"] == "C1" and post["thread_ts"] == "1700000000.000100"
    assert post["text"] == "*Done.* See <https://example.com/d|the doc>."   # Slack's dialect
    assert SlackRelayPost.objects.get(turn=turn).slack_ts == "1700000999.000100"


def test_a_reply_is_never_posted_twice(slack, linked, hal, django_capture_on_commit_callbacks):
    from apps.slack.relay import relay

    mention("hal summarise")
    turn = Turn.objects.get()
    slack.calls.clear()
    _reply(turn, {"kind": "assistant", "payload": {"text": "once"}}, capture=django_capture_on_commit_callbacks)
    relay(turn, list(turn.events.all()))          # a re-delivered signal
    assert len(slack.said("chat.postMessage")) == 1


def test_a_failed_turn_says_so_in_the_thread(slack, linked, hal, django_capture_on_commit_callbacks):
    mention("hal summarise")
    turn = Turn.objects.get()
    slack.calls.clear()
    _reply(turn, {"kind": "status", "payload": {"status": "failed", "result_note": "runner lost"}},
           capture=django_capture_on_commit_callbacks)
    assert "failed: runner lost" in slack.said("chat.postMessage")[0]["text"]


def test_a_dm_reply_goes_to_the_dm_unthreaded(slack, linked, hal, django_capture_on_commit_callbacks):
    event({"type": "message", "channel_type": "im", "user": ALICE, "text": "hal hi",
           "ts": "1700000000.000100", "channel": "D1"})
    slack.calls.clear()
    _reply(Turn.objects.get(), {"kind": "assistant", "payload": {"text": "hello"}},
           capture=django_capture_on_commit_callbacks)
    (post,) = slack.said("chat.postMessage")
    assert post["channel"] == "D1" and "thread_ts" not in post


def test_a_long_reply_is_split_into_whole_posts(slack, linked, hal, django_capture_on_commit_callbacks):
    mention("hal summarise")
    body = "\n\n".join(f"paragraph {i} " + "x" * 900 for i in range(8))
    _reply(Turn.objects.get(), {"kind": "assistant", "payload": {"text": body}},
           capture=django_capture_on_commit_callbacks)
    posts = slack.said("chat.postMessage")
    assert len(posts) > 1 and all(len(p["text"]) <= 3500 for p in posts)
    assert "".join(p["text"] for p in posts).count("paragraph") == 8


def test_a_non_slack_session_is_left_alone(slack, hal, alice, django_capture_on_commit_callbacks):
    from apps.canopy_sessions import services as session_services

    session = session_services.create_session(workspace=hal.workspace, created_by=alice, agent=hal)
    _msg, turn = session_services.send_message(session=session, text="hi", user=alice)
    _reply(turn, {"kind": "assistant", "payload": {"text": "web only"}}, capture=django_capture_on_commit_callbacks)
    assert not slack.said("chat.postMessage")


def test_a_relay_that_fails_is_logged_and_does_not_break_the_append(slack, linked, hal, ws,
                                                                      django_capture_on_commit_callbacks):
    mention("hal summarise")
    turn = Turn.objects.get()
    slack.fail["chat.postMessage"] = "not_in_channel"
    _reply(turn, {"kind": "assistant", "payload": {"text": "lost"}}, capture=django_capture_on_commit_callbacks)
    assert turn.events.filter(kind="assistant").exists()          # the ledger still has it
    assert "not_in_channel" in SlackRelayPost.objects.get(turn=turn).error
    assert Event.objects.filter(workspace=ws, kind="slack.relay_failed").exists()


# ---- back and forth: plain replies in a thread canopy is in ------------------------

def thread_reply(text: str, *, user=ALICE, ts="1700000050.000100", thread_ts="1700000000.000100"):
    return event({"type": "message", "channel_type": "channel", "user": user, "text": text,
                  "ts": ts, "thread_ts": thread_ts, "channel": "C1"})


def test_a_plain_reply_in_the_thread_continues_the_conversation(slack, linked, hal):
    mention("hal first")
    thread_reply("and what about next week?")
    turns = list(Turn.objects.order_by("created_at"))
    assert len(turns) == 2 and turns[0].chat_session_id == turns[1].chat_session_id
    assert turns[1].prompt == "and what about next week?"
    # Every message gets its own public status line; nothing private.
    assert len(slack.said("chat.postMessage")) == 2 and not slack.said("chat.postEphemeral")


def test_messages_in_threads_canopy_is_not_in_are_dropped_unread(slack, linked, hal):
    thread_reply("colleagues talking among themselves", thread_ts="1699999999.000100")
    event({"type": "message", "channel_type": "channel", "user": ALICE, "text": "hal top level",
           "ts": "1700000070.000100", "channel": "C1"})
    assert not Turn.objects.exists() and not Session.objects.exists()
    assert not slack.calls                      # not even a users.info lookup
    assert not Event.objects.filter(source="slack").exists()


def test_a_reply_that_mentions_the_bot_is_handled_once(slack, linked, hal):
    mention("hal first")
    text = f"<@{BOT}> hal again"
    mention(text, ts="1700000050.000100", thread_ts="1700000000.000100")
    thread_reply(text)                          # Slack sends both events for this one message
    assert Turn.objects.count() == 2


# ---- a blocked agent's question, over Slack ----------------------------------------
#
# The question reaches canopy through the runner's session REPORT, the same POST
# that puts it on the phone, so these drive that endpoint rather than setting
# `pending_question` by hand.

from django.utils import timezone  # noqa: E402

from apps.canopy_sessions.models import RunnerBinding  # noqa: E402
from apps.harness.models import Runner  # noqa: E402

MENU = {
    "question": "How should the run proceed?", "title": "Phase 3→4", "body": "", "selected": None,
    "options": [{"number": 1, "label": "Proceed to Phase 4", "description": "carry on"},
                {"number": 2, "label": "Stop the run here", "description": ""}],
    "source": "hook",
}


@pytest.fixture
def bound(slack, linked, hal, alice):
    """A Slack thread whose session a runner is driving (task `c-hal-slack`)."""
    mention("hal run it")
    session = Session.objects.get()
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", paired_by=alice,
                                   workspace=hal.workspace, status=Runner.ONLINE,
                                   last_heartbeat_at=timezone.now())
    RunnerBinding.objects.create(session=session, runner=runner, session_key="c-hal-slack",
                                 emdash_project="hal", thread_key=str(session.id))
    slack.calls.clear()
    return session, runner, alice


def _report(runner, pairer, question, capture, observed_at=1.0):
    c = Client()
    c.force_login(pairer)
    task = {"emdash_task": "c-hal-slack", "project": "hal"}
    if question:
        task["question"] = {**question, "observed_at": observed_at}
    with capture(execute=True):
        assert c.post(f"/api/harness/runners/{runner.id}/sessions", {"sessions": [task]},
                      content_type="application/json").status_code == 200


def test_the_question_is_posted_into_the_thread_once(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    # Re-reported every ~10s, re-stamped by some producers: still ONE post.
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks, observed_at=2.0)
    (post,) = slack.said("chat.postMessage")
    assert post["thread_ts"] == "1700000000.000100"
    assert "waiting on you" in post["text"] and "How should the run proceed?" in post["text"]
    assert "`1` Proceed to Phase 4 — carry on" in post["text"]


def test_the_same_question_asked_again_later_is_posted_again(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    _report(runner, pairer, None, django_capture_on_commit_callbacks)          # answered at the laptop
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks, observed_at=9.0)
    assert len(slack.said("chat.postMessage")) == 2


def test_a_number_in_the_thread_answers_it(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    thread_reply("2", ts="1700000200.000100")
    binding = RunnerBinding.objects.get(session=session)
    assert binding.pending_answer["option"] == 2 and binding.pending_answer["selections"] == [[2]]
    assert Turn.objects.count() == 1                 # an answer is not a new message to the agent
    assert "Answered: Stop the run here" in slack.said("chat.postEphemeral")[-1]["text"]


def test_cancel_dismisses_it(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    thread_reply("cancel", ts="1700000200.000100")
    assert RunnerBinding.objects.get(session=session).pending_answer["option"] is None


def test_anything_else_while_it_waits_is_not_sent(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    for reply in ("what do you recommend?", "3", "1,2"):
        thread_reply(reply, ts=f"17000002{len(reply):02d}.000100")
    assert Turn.objects.count() == 1 and RunnerBinding.objects.get(session=session).pending_answer is None
    assert "reply with the option number" in slack.said("chat.postEphemeral")[-1]["text"]


def test_multi_select_and_several_questions(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    two = {**MENU, "questions": [
        {"index": 0, "question": "Colours?", "header": "", "multi_select": True,
         "options": [{"number": 1, "label": "Red"}, {"number": 2, "label": "Blue"}, {"number": 3, "label": "Green"}]},
        {"index": 1, "question": "Ship?", "header": "", "multi_select": False,
         "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}]},
    ]}
    _report(runner, pairer, two, django_capture_on_commit_callbacks)
    assert "one answer per question" in slack.said("chat.postMessage")[0]["text"]
    thread_reply("1, 3; 2", ts="1700000200.000100")
    assert RunnerBinding.objects.get(session=session).pending_answer["selections"] == [[1, 3], [2]]


def test_a_question_canopy_cannot_read_is_shown_but_does_not_capture_replies(
        bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    marker = {"question": "Claude needs your permission to use Bash", "title": "Waiting on you",
              "body": "", "selected": None, "options": [], "source": "notification"}
    _report(runner, pairer, marker, django_capture_on_commit_callbacks)
    assert "can't read the options" in slack.said("chat.postMessage")[0]["text"]
    thread_reply("go ahead", ts="1700000200.000100")
    assert Turn.objects.count() == 2                 # an ordinary message, as on canopy-web


def test_a_refused_answer_comes_back_to_the_thread(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    _report(runner, pairer, {**MENU, "answer_error": "wrong_pane",
                             "answer_note": "The terminal shown was not the Claude pane."},
            django_capture_on_commit_callbacks)
    assert "didn't land: The terminal shown" in slack.said("chat.postMessage")[-1]["text"]


def test_parse_answer_is_strict():
    from apps.slack.menus import CANCEL, parse_answer

    assert parse_answer("2", MENU) == [[2]]
    assert parse_answer(" 1. ", MENU) == [[1]]
    assert parse_answer("Cancel", MENU) == CANCEL
    for bad in ("", "0", "3", "1 2", "two", "2 please", "1;2"):
        assert parse_answer(bad, MENU) is None, bad


# ---- buttons: the question as Block Kit, answered by a click ------------------------

def click(action_id, value, *, state=None, user=ALICE, channel="C1", message_ts="1700000999.000100",
          thread_ts="1700000000.000100"):
    from urllib.parse import urlencode
    payload = {"type": "block_actions", "team": {"id": TEAM}, "user": {"id": user},
               "channel": {"id": channel},
               "container": {"type": "message", "message_ts": message_ts, "channel_id": channel},
               "message": {"ts": message_ts, "thread_ts": thread_ts},
               "actions": [{"action_id": action_id, "value": value}],
               "state": state or {"values": {}}}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return _post("/api/slack/interactions", body, "application/x-www-form-urlencoded")


def _buttons(post):
    return {e["action_id"]: e for b in post.get("blocks", []) if b["type"] == "actions" for e in b["elements"]}


def test_the_question_comes_with_a_button_per_option(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    (post,) = slack.said("chat.postMessage")
    buttons = _buttons(post)
    assert set(buttons) == {"menu_pick_1", "menu_pick_2", "menu_dismiss"}
    assert buttons["menu_pick_2"]["text"]["text"] == "Stop the run here"
    assert "How should the run proceed?" in post["text"]           # the fallback is still there


def test_a_click_answers_it_and_the_buttons_go_away(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    value = _buttons(slack.said("chat.postMessage")[0])["menu_pick_2"]["value"]
    assert click("menu_pick_2", value).status_code == 200
    assert RunnerBinding.objects.get(session=session).pending_answer["selections"] == [[2]]
    (update,) = slack.said("chat.update")
    assert update["ts"] == "1700000999.000100"
    assert f"Answered by <@{ALICE}>: Stop the run here" in update["text"]
    assert not [b for b in update["blocks"] if b["type"] == "actions"]
    # When the dialog then clears, that "Answered by" is not overwritten.
    _report(runner, pairer, None, django_capture_on_commit_callbacks)
    assert len(slack.said("chat.update")) == 1


def test_a_question_answered_elsewhere_loses_its_buttons(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    _report(runner, pairer, None, django_capture_on_commit_callbacks)      # answered at the laptop
    (update,) = slack.said("chat.update")
    assert "Answered." in update["text"] and not [b for b in update["blocks"] if b["type"] == "actions"]


def test_a_click_on_a_question_that_moved_on_answers_nothing(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    old = _buttons(slack.said("chat.postMessage")[0])["menu_pick_1"]["value"]
    _report(runner, pairer, {**MENU, "question": "A different question now?"}, django_capture_on_commit_callbacks)
    click("menu_pick_1", old)
    assert RunnerBinding.objects.get(session=session).pending_answer is None
    assert "no longer open" in slack.said("chat.postEphemeral")[-1]["text"]


def test_a_click_naming_another_channels_session_answers_nothing(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    value = _buttons(slack.said("chat.postMessage")[0])["menu_pick_1"]["value"]
    click("menu_pick_1", value, channel="C_OTHER")
    assert RunnerBinding.objects.get(session=session).pending_answer is None


def test_pick_any_and_several_questions_submit_their_state(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    two = {**MENU, "questions": [
        {"index": 0, "question": "Colours?", "header": "", "multi_select": True,
         "options": [{"number": 1, "label": "Red"}, {"number": 2, "label": "Blue"}, {"number": 3, "label": "Green"}]},
        {"index": 1, "question": "Ship?", "header": "", "multi_select": False,
         "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}]},
    ]}
    _report(runner, pairer, two, django_capture_on_commit_callbacks)
    post = slack.said("chat.postMessage")[0]
    kinds = {e["action_id"]: e["type"] for b in post["blocks"] if b["type"] == "actions" for e in b["elements"]}
    assert kinds == {"q0": "checkboxes", "q1": "radio_buttons", "menu_submit": "button", "menu_dismiss": "button"}
    # A toggle is only a change of selection — nothing happens until Submit,
    # and nothing is said either (a note per tick would be noise).
    before = len(slack.calls)
    click("q0", "")
    assert RunnerBinding.objects.get(session=session).pending_answer is None
    assert len(slack.calls) == before
    state = {"values": {
        "menu_q0": {"q0": {"type": "checkboxes", "selected_options": [{"value": "1"}, {"value": "3"}]}},
        "menu_q1": {"q1": {"type": "radio_buttons", "selected_option": {"value": "2"}}},
    }}
    click("menu_submit", _buttons(post)["menu_submit"]["value"], state=state)
    assert RunnerBinding.objects.get(session=session).pending_answer["selections"] == [[1, 3], [2]]


def test_submit_with_a_question_unanswered_is_refused(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    two = {**MENU, "questions": [
        {"index": 0, "question": "Ship?", "header": "", "multi_select": False,
         "options": [{"number": 1, "label": "Yes"}, {"number": 2, "label": "No"}]},
        {"index": 1, "question": "When?", "header": "", "multi_select": False,
         "options": [{"number": 1, "label": "Now"}, {"number": 2, "label": "Later"}]},
    ]}
    _report(runner, pairer, two, django_capture_on_commit_callbacks)
    value = _buttons(slack.said("chat.postMessage")[0])["menu_submit"]["value"]
    click("menu_submit", value, state={"values": {"menu_q0": {"q0": {"selected_option": {"value": "1"}}}}})
    assert RunnerBinding.objects.get(session=session).pending_answer is None
    assert "each question" in slack.said("chat.postEphemeral")[-1]["text"]


def test_dismiss_button(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    click("menu_dismiss", _buttons(slack.said("chat.postMessage")[0])["menu_dismiss"]["value"])
    assert RunnerBinding.objects.get(session=session).pending_answer["option"] is None


def test_an_unsigned_click_is_refused(bound, slack):
    from urllib.parse import urlencode
    body = urlencode({"payload": "{}"}).encode()
    assert _post("/api/slack/interactions", body, "application/x-www-form-urlencoded",
                 secret="wrong").status_code == 401


# ---- speaking as the agent, and /<agent> commands -----------------------------------

def test_replies_are_posted_as_the_agent(slack, linked, hal, django_capture_on_commit_callbacks):
    hal.avatar_url = "https://example.com/hal.png"
    hal.save()
    mention("hal summarise")
    slack.calls.clear()                            # the status line is canopy's, not the agent's
    _reply(Turn.objects.get(), {"kind": "assistant", "payload": {"text": "hi"}},
           capture=django_capture_on_commit_callbacks)
    (post,) = slack.said("chat.postMessage")
    assert (post["username"], post["icon_url"]) == ("Hal", "https://example.com/hal.png")


def test_an_install_without_the_customize_scope_still_gets_the_reply(slack, linked, hal,
                                                                     django_capture_on_commit_callbacks):
    real = slack.__call__

    def no_customize(url, headers=None, json=None, data=None, timeout=None):
        if url.endswith("chat.postMessage") and json and "username" in json:
            slack.calls.append(("chat.postMessage", json))
            resp = mock.Mock(status_code=200)
            resp.raise_for_status = lambda: None
            resp.json = lambda: {"ok": False, "error": "missing_scope"}
            return resp
        return real(url, headers=headers, json=json, data=data, timeout=timeout)

    mention("hal summarise")
    slack.calls.clear()
    with mock.patch("apps.slack.client.requests.post", side_effect=no_customize):
        _reply(Turn.objects.get(), {"kind": "assistant", "payload": {"text": "hi"}},
               capture=django_capture_on_commit_callbacks)
    posts = slack.said("chat.postMessage")
    assert len(posts) == 2 and "username" not in posts[-1] and posts[-1]["text"] == "hi"


def _agent_command(command: str, text: str, user=ALICE):
    from urllib.parse import urlencode
    body = urlencode({"team_id": TEAM, "channel_id": "C1", "user_id": user,
                      "command": command, "text": text}).encode()
    return _post("/api/slack/commands", body, "application/x-www-form-urlencoded")


def test_a_command_named_after_an_agent_goes_to_that_agent(slack, linked, hal):
    _agent_command("/hal", "what's on my plate?")
    turn = Turn.objects.get()
    assert turn.chat_session.agent == hal and turn.prompt == "what's on my plate?"


def test_a_command_for_an_agent_not_on_for_slack_does_nothing(slack, linked, ws):
    Agent.objects.create(slug="ace", name="ACE", workspace=ws, slack_enabled=False)
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, slack_enabled=True)
    resp = _agent_command("/ace", "run it")
    assert not Turn.objects.exists() and "`hal`" in resp.json()["text"]


def test_a_bare_agent_command_says_how_to_use_it(slack, linked, hal):
    assert "`/hal <ask>`" in _agent_command("/hal", "").json()["text"]


# ---- the status line: working on it, or blocked and why ----------------------
#
# Driven through real routing: a runner with an assignment for `hal`, online or
# with a lapsed heartbeat, and the real claim/finish calls a runner's POSTs land
# on — so "picked up" is asserted against what claiming actually does.

import datetime as _dt  # noqa: E402

from apps.harness.models import RunnerAdmin, RunnerAssignment  # noqa: E402
from apps.slack.models import SlackTurnPost  # noqa: E402


def _runner(name, *, kind=Runner.EMDASH, online=True, pairer, agent=None):
    beat = timezone.now() - (_dt.timedelta(0) if online else _dt.timedelta(hours=2))
    r = Runner.objects.create(name=name, kind=kind, host=name, paired_by=pairer, workspace_id=pairer_ws(pairer),
                              status=Runner.ONLINE, last_heartbeat_at=beat,
                              capabilities={"sessions": True})
    if agent is not None:
        RunnerAssignment.objects.create(agent=agent, runner=r, rank=0)
    return r


def pairer_ws(user):
    return WorkspaceMembership.objects.filter(user=user).values_list("workspace_id", flat=True).first()


def _line(slack):
    return slack.said("chat.postMessage")[-1]


def test_a_live_runner_says_it_is_picking_it_up(slack, linked, hal, alice):
    _runner("jj-mbp", pairer=alice, agent=hal)
    mention("hal summarise")
    line = _line(slack)
    assert "`hal` is picking this up on *jj-mbp*" in line["text"]
    assert "Open in canopy" in line["text"] and not line.get("blocks")


def test_an_offline_runner_says_it_is_blocked(slack, linked, hal, alice):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    mention("hal summarise")
    assert "*jj-mbp* is offline" in _line(slack)["text"]
    assert not _line(slack).get("blocks")          # no cloud runner, so no button


def test_the_line_is_edited_as_the_turn_moves(slack, linked, hal, alice, django_capture_on_commit_callbacks):
    runner = _runner("jj-mbp", pairer=alice, agent=hal)
    mention("hal summarise")
    with django_capture_on_commit_callbacks(execute=True):
        turn = harness_services.claim_next_turn(runner)
    assert turn is not None
    assert "working on this on *jj-mbp*" in slack.said("chat.update")[-1]["text"]
    with django_capture_on_commit_callbacks(execute=True):
        harness_services.finish_turn(turn, status=Turn.DONE)
    assert "finished this on *jj-mbp*" in slack.said("chat.update")[-1]["text"]
    assert slack.said("chat.update")[-1]["ts"] == SlackTurnPost.objects.get(turn=turn).slack_ts
    assert len(slack.said("chat.postMessage")) == 1          # one line per ask, edited in place


@pytest.fixture
def cloud(ws, hal):
    """An online cloud runner owned by someone else, and an offline laptop for hal."""
    owner = a_user("ops@dimagi.com")
    wsvc.ensure_member(ws, owner, WorkspaceMembership.EDITOR)
    return _runner("cloud-ec2-1", kind=Runner.CLOUD, pairer=owner)


def _route_button(post):
    return next(e for b in post.get("blocks", []) if b["type"] == "actions" for e in b["elements"]
                if e["action_id"] == "route_cloud")


def test_blocked_with_a_cloud_runner_offers_the_button(slack, linked, hal, alice, cloud):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    mention("hal summarise")
    line = _line(slack)
    assert "send it to *cloud-ec2-1*" in line["text"]
    assert _route_button(line)["text"]["text"] == "Run on cloud-ec2-1"


def test_only_a_cloud_runner_admin_may_press_it(slack, linked, hal, alice, cloud):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    mention("hal summarise")
    value = _route_button(_line(slack))["value"]
    click("route_cloud", value)
    turn = Turn.objects.get()
    assert turn.pinned_runner_id is None
    assert "Only an admin of *cloud-ec2-1*" in slack.said("chat.postEphemeral")[-1]["text"]
    assert Event.objects.filter(kind="slack.forbidden").exists()


def test_an_admin_sends_an_unbound_conversation_to_the_cloud(slack, linked, hal, alice, cloud):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    mention("hal summarise")
    click("route_cloud", _route_button(_line(slack))["value"])
    turn = Turn.objects.get()
    assert turn.pinned_runner == cloud
    assert turn.chat_session.metadata["requested_runner_id"] == str(cloud.id)   # later sends follow
    assert "Sent to *cloud-ec2-1*" in slack.said("chat.update")[-1]["text"]
    assert not [b for b in slack.said("chat.update")[-1]["blocks"] if b["type"] == "actions"]
    # And the cloud box really claims it.
    assert harness_services.claim_next_turn(cloud) == turn


def test_a_bound_conversation_is_transferred_and_the_handoff_runs_first(
        slack, linked, hal, alice, cloud, django_capture_on_commit_callbacks):
    laptop = _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    mention("hal first")
    session = Session.objects.get()
    RunnerBinding.objects.create(session=session, runner=laptop, session_key="c-hal",
                                 emdash_project="hal", thread_key=str(session.id))
    mention("and then this", ts="1700000050.000100", thread_ts="1700000000.000100")
    click("route_cloud", _route_button(_line(slack))["value"])
    assert RunnerBinding.objects.get(session=session).runner == cloud
    first = harness_services.claim_next_turn(cloud)
    assert "transferred from jj-mbp to cloud-ec2-1" in first.prompt
    assert "2 message(s) waiting" in first.prompt


def test_slash_cloud_moves_everything_of_mine_that_is_stuck(slack, linked, hal, alice, cloud):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    mention("hal one", ts="1700000000.000100")
    mention("hal two", ts="1700000100.000100")
    resp = command("cloud")
    assert "Sent 1 waiting message(s) to *cloud-ec2-1*" in resp.json()["text"]
    assert set(Turn.objects.values_list("pinned_runner_id", flat=True)) == {cloud.id}


def test_mention_cloud_is_the_same_and_leaves_live_work_alone(slack, linked, hal, alice, cloud):
    _runner("jj-mbp", pairer=alice, agent=hal)                  # online: nothing is stuck
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    mention("hal one")
    mention("cloud", ts="1700000200.000100")
    assert "Nothing of yours is waiting" in slack.said("chat.postEphemeral")[-1]["text"]
    assert Turn.objects.get().pinned_runner_id is None


# ---- the conversation carrying on somewhere else --------------------------------

def test_a_turn_sent_from_canopy_web_is_announced_in_the_thread(
        slack, linked, hal, alice, django_capture_on_commit_callbacks):
    from apps.canopy_sessions import services as session_services

    runner = _runner("jj-mbp", pairer=alice, agent=hal)
    mention("hal first")
    session = Session.objects.get()
    with django_capture_on_commit_callbacks(execute=True):
        harness_services.finish_turn(harness_services.claim_next_turn(runner), status=Turn.DONE)
    slack.calls.clear()
    session_services.send_message(session=session, text="now do the second half", user=alice)
    with django_capture_on_commit_callbacks(execute=True):
        harness_services.claim_next_turn(runner)
    (line,) = slack.said("chat.postMessage")
    assert line["thread_ts"] == "1700000000.000100"
    assert "continued this in canopy: _now do the second half_" in line["text"]
    assert "working on this on *jj-mbp*" in line["text"]


def _stream(runner, pairer, session, events, capture):
    c = Client()
    c.force_login(pairer)
    with capture(execute=True):
        resp = c.post(f"/api/harness/runners/{runner.id}/session-stream",
                      {"session_id": str(session.id), "transcript_id": "t1", "events": events},
                      content_type="application/json")
    assert resp.status_code == 200, resp.content


def test_typing_straight_into_emdash_is_announced_once(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    session.metadata = {**session.metadata, "transcript_sourced": True}
    session.save()
    Turn.objects.filter(chat_session=session).update(status=Turn.DONE)
    ev = lambda i, kind, text: {"seq": i, "index": i * 1000, "kind": kind, "payload": {"text": text}}  # noqa: E731
    _stream(runner, pairer, session, [ev(1, "user", "actually, check the logs first")],
            django_capture_on_commit_callbacks)
    _stream(runner, pairer, session, [ev(2, "user", "and the metrics")], django_capture_on_commit_callbacks)
    (note,) = slack.said("chat.postMessage")
    assert "carrying on directly in the agent's session on *jj-mbp*" in note["text"]
    assert note["thread_ts"] == "1700000000.000100"


def test_a_prompt_delivered_by_a_slack_turn_is_not_announced(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    session.metadata = {**session.metadata, "transcript_sourced": True}
    session.save()
    Turn.objects.filter(chat_session=session).update(status=Turn.DONE)
    _stream(runner, pairer, session, [{"seq": 1, "index": 1000, "kind": "user",
                                       "payload": {"text": "run it"}}], django_capture_on_commit_callbacks)
    assert not slack.said("chat.postMessage")


# ---- a runner that goes away MID-turn --------------------------------------------
#
# The laptop-lid case. Unlike a queued turn on an offline box, nothing marks the
# moment: the turn was claimed, reads RUNNING, and the runner simply stops
# heartbeating. The line must notice on its own (the sweep), ping the thread,
# offer the move, and ping again if the box returns.

from apps.slack import status as slack_status  # noqa: E402


def _lapse(runner):
    Runner.objects.filter(pk=runner.pk).update(last_heartbeat_at=timezone.now() - _dt.timedelta(minutes=5))


def _revive(runner):
    Runner.objects.filter(pk=runner.pk).update(last_heartbeat_at=timezone.now())


def _claimed(slack, hal, alice, capture, text="hal summarise"):
    runner = _runner("jj-mbp", pairer=alice, agent=hal)
    mention(text)
    with capture(execute=True):
        turn = harness_services.claim_next_turn(runner)
    assert turn is not None
    return runner, turn


def _pings(slack, needle):
    return [p for p in slack.said("chat.postMessage") if needle in p["text"]]


def test_a_runner_dying_mid_turn_is_noticed_and_pinged_once(slack, linked, hal, alice, cloud,
                                                            django_capture_on_commit_callbacks):
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    slack_status.sweep(force=True)
    slack_status.sweep(force=True)                 # the next pass: no second ping

    line = slack.said("chat.update")[-1]
    assert "Paused — *jj-mbp* went offline while `hal` was working" in line["text"]
    assert _route_button(line)["text"]["text"] == "Run on cloud-ec2-1"
    (ping,) = _pings(slack, "went offline while")
    assert ping["thread_ts"] == "1700000000.000100"


def test_the_runner_coming_back_is_pinged_and_the_line_recovers(slack, linked, hal, alice,
                                                                django_capture_on_commit_callbacks):
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    slack_status.sweep(force=True)
    _revive(runner)
    slack_status.sweep(force=True)

    assert _pings(slack, "*jj-mbp* is back online")
    assert "working on this on *jj-mbp*" in slack.said("chat.update")[-1]["text"]
    assert SlackTurnPost.objects.get(turn=turn).offline_notice_ts == ""
    _lapse(runner)                                 # a second outage pings again
    slack_status.sweep(force=True)
    assert len(_pings(slack, "went offline while")) == 2


def test_the_sweep_rides_other_runners_reports(slack, linked, hal, alice, cloud,
                                               django_capture_on_commit_callbacks):
    from django.core.cache import cache

    from apps.harness.signals import sessions_reported

    runner, _turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    cache.delete(slack_status.SWEEP_LOCK)
    sessions_reported.send(sender=Runner, runner=cloud)       # the cloud box's routine report
    assert _pings(slack, "went offline while")


def test_an_admin_moves_a_stranded_turn_to_the_cloud(slack, linked, hal, alice, cloud,
                                                     django_capture_on_commit_callbacks):
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    slack_status.sweep(force=True)
    value = _route_button(slack.said("chat.update")[-1])["value"]

    with django_capture_on_commit_callbacks(execute=True):
        click("route_cloud", value)

    turn.refresh_from_db()
    assert turn.status == Turn.LOST                           # the dead box lets go
    again = Turn.objects.exclude(pk=turn.pk).get(prompt="summarise")
    assert again.pinned_runner == cloud                       # the same ask, on the cloud
    assert harness_services.claim_next_turn(cloud) == again
    # The thread sees the new attempt, and the old line loses its button.
    assert SlackTurnPost.objects.filter(turn=again).exclude(slack_ts="").exists()
    old = [u for u in slack.said("chat.update") if u["ts"] == SlackTurnPost.objects.get(turn=turn).slack_ts][-1]
    assert "went away before it was done" in old["text"]
    assert not [b for b in old["blocks"] if b["type"] == "actions"]


def test_a_stranded_turn_is_not_moved_by_a_non_admin(slack, linked, hal, alice, cloud,
                                                     django_capture_on_commit_callbacks):
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    slack_status.sweep(force=True)
    click("route_cloud", _route_button(slack.said("chat.update")[-1])["value"])
    turn.refresh_from_db()
    assert turn.status == Turn.CLAIMED and Turn.objects.count() == 1


def test_a_move_leaves_a_runner_that_is_still_there_alone(slack, linked, hal, alice, cloud,
                                                          django_capture_on_commit_callbacks):
    """A stale button pressed after the laptop came back must not kill live work."""
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    slack_status.sweep(force=True)
    value = _route_button(slack.said("chat.update")[-1])["value"]
    _revive(runner)
    click("route_cloud", value)
    turn.refresh_from_db()
    assert turn.status == Turn.CLAIMED and Turn.objects.count() == 1


def test_a_lost_turn_can_be_run_again_on_the_cloud(slack, linked, hal, alice, cloud,
                                                   django_capture_on_commit_callbacks):
    RunnerAdmin.objects.create(runner=cloud, user=alice)
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    _lapse(runner)
    Turn.objects.filter(pk=turn.pk).update(lease_expires_at=timezone.now() - _dt.timedelta(seconds=1))
    with django_capture_on_commit_callbacks(execute=True):
        harness_services.sweep_expired_leases()               # what the fleet does 15 min later
    line = slack.said("chat.update")[-1]
    assert "went away before it was done" in line["text"]
    with django_capture_on_commit_callbacks(execute=True):
        click("route_cloud", _route_button(line)["value"])
    again = Turn.objects.exclude(pk=turn.pk).get(prompt="summarise")
    assert again.pinned_runner == cloud


# ---- text the agent writes after its turn closed ---------------------------------
#
# emdash ends a turn when the agent yields to background work, so the ledger relay
# posts what was written up to then; the real answer (after CI, a merge, a deploy)
# arrives only on the transcript stream. 2026-09-19: the summary of the change that
# built the status line never reached the thread that asked for it.

def _yielded(bound, capture, bridged="Waiting on CI — back when it lands."):
    """The Slack turn delivered, bridged its reply so far, then closed on a yield."""
    session, runner, pairer = bound
    session.metadata = {**session.metadata, "transcript_sourced": True}
    session.save()
    turn = Turn.objects.get(chat_session=session)
    _reply(turn, {"kind": "assistant", "payload": {"text": bridged}}, capture=capture)
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    _stream(runner, pairer, session, [
        {"seq": 1, "index": 1000, "kind": "user", "payload": {"text": "run it"}},
        {"seq": 2, "index": 2000, "kind": "assistant", "payload": {"text": bridged}},
    ], capture)
    return session, runner, pairer


def test_the_answer_written_after_the_turn_closed_reaches_the_thread(
        bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = _yielded(bound, django_capture_on_commit_callbacks)
    slack.calls.clear()
    done = {"seq": 3, "index": 3000, "kind": "assistant", "payload": {"text": "**Merged** and deployed."}}
    _stream(runner, pairer, session, [done], django_capture_on_commit_callbacks)
    _stream(runner, pairer, session, [done], django_capture_on_commit_callbacks)      # re-shipped batch
    (post,) = slack.said("chat.postMessage")
    assert post["text"] == "*Merged* and deployed." and post["thread_ts"] == "1700000000.000100"
    assert post["username"] == "Hal"                                                  # as the agent


def test_text_already_bridged_by_the_turn_is_not_posted_again(bound, slack, django_capture_on_commit_callbacks):
    _yielded(bound, django_capture_on_commit_callbacks)
    # The yield's own text arrived on the stream too; only the ledger's copy was posted.
    assert [p["text"] for p in slack.said("chat.postMessage")].count("Waiting on CI — back when it lands.") == 1


def test_a_reply_to_something_typed_in_emdash_is_not_mirrored(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = _yielded(bound, django_capture_on_commit_callbacks)
    slack.calls.clear()
    _stream(runner, pairer, session, [
        {"seq": 3, "index": 3000, "kind": "user", "payload": {"text": "private aside, just for me"}},
        {"seq": 4, "index": 4000, "kind": "assistant", "payload": {"text": "sure — here's the aside"}},
    ], django_capture_on_commit_callbacks)
    (note,) = slack.said("chat.postMessage")
    assert "outside Slack" in note["text"]                       # announced, not mirrored


def test_nothing_is_relayed_this_way_while_a_turn_is_running(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = _yielded(bound, django_capture_on_commit_callbacks)
    Turn.objects.filter(chat_session=session).update(status=Turn.RUNNING)
    slack.calls.clear()
    _stream(runner, pairer, session, [{"seq": 3, "index": 3000, "kind": "assistant",
                                       "payload": {"text": "mid-turn text"}}], django_capture_on_commit_callbacks)
    assert not slack.said("chat.postMessage")                    # the ledger relay owns a live turn


# ---- canopy keeps the app's /<agent> commands in step (apps/slack/commands.py) -------

def _commands(slack) -> dict[str, dict]:
    return {c["command"]: c for c in slack.manifest["features"]["slash_commands"]}


@pytest.fixture
def owner_client(ws):
    return _link_client(_owner(ws))


@pytest.fixture
def managed(slack, installation, owner_client, ws):
    installation.app_id = "A_CANOPY"
    installation.save()
    resp = owner_client.put(f"/api/slack-config/{ws.slug}/config-token", {"refresh_token": "xoxe-pasted"},
                            content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_connecting_the_token_rotates_it_and_syncs_enabled_agents(slack, hal, installation, managed):
    rotate = slack.said("tooling.tokens.rotate")[0]
    assert rotate["refresh_token"] == "xoxe-pasted"
    installation.refresh_from_db()
    # The pasted refresh token is spent; the NEW pair is what is kept, encrypted.
    from apps.common.encryption import decrypt_secret as _dec
    assert installation.config_refresh_enc and "xoxe" not in installation.config_refresh_enc
    assert _dec(installation.config_refresh_enc) == "xoxe-refresh-1"
    assert managed["status"] == "synced" and managed["added"] == ["/hal"]
    cmds = _commands(slack)
    assert set(cmds) == {"/canopy", "/standup", "/hal"}
    assert cmds["/hal"]["url"] == "https://canopy.test/canopy/api/slack/commands"
    # Everything else in the manifest is written back untouched.
    assert slack.manifest["settings"]["event_subscriptions"]["bot_events"] == ["app_mention", "message.im"]
    assert cmds["/standup"]["url"] == "https://elsewhere.example/standup"


def test_flipping_the_switch_adds_and_removes_the_command(slack, hal, managed, owner_client):
    off = owner_client.patch("/api/agents/hal/slack", {"slack_enabled": False},
                             content_type="application/json").json()
    assert off == {"slack_enabled": False, "command_status": "synced", "command_detail": "Removed /hal from Slack."}
    assert "/hal" not in _commands(slack) and "/canopy" in _commands(slack)
    on = owner_client.patch("/api/agents/hal/slack", {"slack_enabled": True},
                            content_type="application/json").json()
    assert on["command_detail"] == "Added /hal to Slack." and "/hal" in _commands(slack)


def test_a_command_canopy_does_not_own_is_never_removed(slack, ws, managed, owner_client):
    # `/hal` exists but points somewhere else: not canopy's to remove.
    Agent.objects.create(slug="hal", name="Hal", workspace=ws, slack_enabled=False)
    slack.manifest["features"]["slash_commands"].append(
        {"command": "/hal", "url": "https://elsewhere.example/hal", "description": "someone else's"})
    owner_client.post(f"/api/slack-config/{ws.slug}/sync")
    assert _commands(slack)["/hal"]["url"] == "https://elsewhere.example/hal"


def test_an_expired_config_token_is_rotated_before_use_and_never_reused(slack, hal, installation, managed,
                                                                          owner_client, ws):
    from django.utils import timezone as tz
    installation.refresh_from_db()
    installation.config_expires_at = tz.now()
    installation.save()
    owner_client.post(f"/api/slack-config/{ws.slug}/sync")
    rotations = slack.said("tooling.tokens.rotate")
    assert len(rotations) == 2 and rotations[1]["refresh_token"] == "xoxe-refresh-1"


def test_without_a_config_token_the_switch_still_works_and_says_so(slack, installation, alice, ws, owner_client):
    Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    resp = owner_client.patch("/api/agents/hal/slack", {"slack_enabled": True},
                              content_type="application/json").json()
    assert resp["slack_enabled"] is True and resp["command_status"] == "not_configured"
    assert Agent.objects.get(slug="hal").slack_enabled is True


def test_when_slack_refuses_the_switch_still_flips_and_the_error_is_kept(slack, hal, installation, managed,
                                                                          owner_client):
    slack.fail["apps.manifest.update"] = "invalid_manifest"
    resp = owner_client.patch("/api/agents/hal/slack", {"slack_enabled": False},
                              content_type="application/json").json()
    assert resp["slack_enabled"] is False and resp["command_status"] == "error"
    installation.refresh_from_db()
    assert "invalid_manifest" in installation.commands_sync_error


def test_a_slug_too_long_for_slack_is_reported_not_truncated(slack, installation, managed, ws, owner_client):
    long = "a" * 40
    Agent.objects.create(slug=long, name="Long", workspace=ws)
    resp = owner_client.patch(f"/api/agents/{long}/slack", {"slack_enabled": True},
                              content_type="application/json").json()
    assert "too long" in resp["command_detail"]
    assert not [c for c in _commands(slack) if c.startswith("/aaa")]


def test_only_an_owner_hands_canopy_the_config_token(slack, installation, alice, ws):
    resp = _link_client(alice).put(f"/api/slack-config/{ws.slug}/config-token",
                                   {"refresh_token": "xoxe-x"}, content_type="application/json")
    assert resp.status_code == 403 and not slack.said("tooling.tokens.rotate")


def test_the_config_read_never_returns_a_token(slack, installation, managed, alice, ws):
    body = _link_client(alice).get(f"/api/slack-config/{ws.slug}").content.decode()
    assert "xoxe" not in body
    assert '"managed":true' in body.replace(" ", "")


def test_install_records_the_app_id(slack, ws):
    c = _link_client(_owner(ws))
    start = c.get("/auth/slack/install/", {"workspace": ws.slug})
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    c.get("/auth/slack/callback/", {"code": "abc", "state": state})
    assert SlackInstallation.objects.get(team_id=TEAM).app_id == "A_CANOPY"


# ---- Slack's OWN working indicator (agent sessions) ------------------------------
#
# The native affordance for "I am doing something": a spinner Slack draws in the
# thread, with a Stop button, driven by agents.sessions.setStatus rather than by
# editing message text. It only renders for an app declared an agent, so every
# call has to degrade to nothing on a deployment where that has not been done.

def _statuses(slack):
    return [p["status"] for p in slack.said("agents.sessions.setStatus")]


def test_the_indicator_follows_the_turn(slack, linked, hal, alice, django_capture_on_commit_callbacks):
    runner = _runner("jj-mbp", pairer=alice, agent=hal)
    mention("hal summarise")
    assert _statuses(slack) == ["processing"]          # queued, a live runner has it
    call = slack.said("agents.sessions.setStatus")[-1]
    assert call["channel_id"] == "C1" and call["thread_ts"] == "1700000000.000100"

    with django_capture_on_commit_callbacks(execute=True):
        turn = harness_services.claim_next_turn(runner)
    assert _statuses(slack)[-1] == "processing"
    with django_capture_on_commit_callbacks(execute=True):
        harness_services.finish_turn(turn, status=Turn.DONE)
    assert _statuses(slack)[-1] == "active"            # the spinner stops when the work does


def test_an_offline_runner_suspends_rather_than_spins(slack, linked, hal, alice):
    _runner("jj-mbp", pairer=alice, agent=hal, online=False)
    mention("hal summarise")
    assert _statuses(slack)[-1] == "suspended"


def test_a_runner_dying_mid_turn_stops_the_spinner(slack, linked, hal, alice,
                                                   django_capture_on_commit_callbacks):
    runner, _turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    assert _statuses(slack)[-1] == "processing"
    _lapse(runner)
    slack_status.sweep(force=True)
    assert _statuses(slack)[-1] == "suspended"         # it needs a person, and says so
    _revive(runner)
    slack_status.sweep(force=True)
    assert _statuses(slack)[-1] == "processing"


def test_a_question_suspends_the_session(bound, slack, django_capture_on_commit_callbacks):
    session, runner, pairer = bound
    # Mid-turn, so "suspended" can only be the QUESTION talking.
    Turn.objects.filter(chat_session=session).update(status=Turn.RUNNING, claimed_by=runner)
    _report(runner, pairer, MENU, django_capture_on_commit_callbacks)
    assert _statuses(slack)[-1] == "suspended"
    _report(runner, pairer, None, django_capture_on_commit_callbacks)
    assert _statuses(slack)[-1] == "processing"        # answered at the keyboard; back to work


def test_an_app_that_is_not_an_agent_yet_still_works(slack, linked, hal, alice):
    """Until the app is declared an agent every call is refused. The thread must
    be exactly as good as it was before — the text line is the load-bearing half."""
    slack.fail["agents.sessions.setStatus"] = "feature_disabled"
    _runner("jj-mbp", pairer=alice, agent=hal)
    mention("hal summarise")
    assert Turn.objects.count() == 1
    assert "is picking this up on *jj-mbp*" in _line(slack)["text"]


def test_a_slack_stop_press_cancels_the_turn(slack, linked, hal, alice,
                                             django_capture_on_commit_callbacks):
    runner, turn = _claimed(slack, hal, alice, django_capture_on_commit_callbacks)
    resp = event({"type": "agent_session_stopped", "channel_id": "C1",
                  "thread_ts": "1700000000.000100", "user": ALICE})
    assert resp.status_code == 200
    turn.refresh_from_db()
    assert turn.events.filter(kind="cancel_requested").exists() or turn.status == Turn.CANCELLED


def test_a_stop_for_a_thread_we_do_not_know_is_harmless(slack, linked, hal, alice):
    assert event({"type": "agent_session_stopped", "channel_id": "CZZZ",
                  "thread_ts": "1700009999.000100", "user": ALICE}).status_code == 200


# ---- declaring the app an agent (the owner-only button) --------------------------
#
# The manifest edits that make Slack draw its own indicator. Driven through the
# API the settings page calls, because the management command beside it cannot be
# run on a deployment with no shell — which is this one.


def _declare(owner_client, ws):
    return owner_client.post(f"/api/slack-config/{ws.slug}/declare-agent")


def test_declaring_makes_the_three_manifest_edits(slack, ws, installation, managed, owner_client):
    body = _declare(owner_client, ws).json()
    assert body["status"] == "declared" and body["reinstall_required"] is True
    assert "/auth/slack/install/" in body["install_url"]

    assert slack.manifest["features"]["agent_view"]["agent_description"]
    assert "assistant:write" in slack.manifest["oauth_config"]["scopes"]["bot"]
    events = slack.manifest["settings"]["event_subscriptions"]["bot_events"]
    assert {"agent_session_stopped", "agent_session_title_changed", "app_context_changed"} <= set(events)
    assert "app_mention" in events and "message.im" in events    # what was there is kept
    assert "/standup" in {c["command"] for c in slack.manifest["features"]["slash_commands"]}

    installation.refresh_from_db()
    assert installation.agent_declared_at is not None
    assert owner_client.get(f"/api/slack-config/{ws.slug}").json()["agent"]["declared"] is True


def test_declaring_twice_writes_nothing_the_second_time(slack, ws, installation, managed, owner_client):
    _declare(owner_client, ws)
    writes = len(slack.said("apps.manifest.update"))
    body = _declare(owner_client, ws).json()
    assert body["status"] == "already_declared" and body["changed"] == []
    assert len(slack.said("apps.manifest.update")) == writes     # idempotent


def test_an_app_on_the_older_assistant_view_is_refused(slack, ws, installation, managed, owner_client):
    slack.manifest["features"]["assistant_view"] = {"assistant_description": "old"}
    writes = len(slack.said("apps.manifest.update"))
    assert _declare(owner_client, ws).status_code == 422          # one-way switch: a human decides
    assert len(slack.said("apps.manifest.update")) == writes


def test_only_an_owner_may_declare(slack, ws, installation, managed, alice):
    member = _link_client(alice)                                  # a member, not an owner
    writes = len(slack.said("apps.manifest.update"))
    assert member.post(f"/api/slack-config/{ws.slug}/declare-agent").status_code == 403
    assert len(slack.said("apps.manifest.update")) == writes


def test_declaring_without_a_config_token_says_so(slack, ws, installation, owner_client):
    assert _declare(owner_client, ws).status_code == 409           # nothing to edit the app with
