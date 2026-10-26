"""The Slack front door's test world: a faked Slack Web API, an installation, a linked
member and an agent, and the signed-request helpers that drive apps/slack end to end.
Registered as a pytest plugin (root conftest.py), so its fixtures need no import; a
suite that wants the Slack settings asks for `slack_configured`
(`pytest.mark.usefixtures`).
"""
from __future__ import annotations

import json
import json as _json
import time
from unittest import mock

import pytest
from django.test import Client

from apps.agents.models import Agent
from apps.slack.models import (
    SlackInstallation,
    SlackUserLink,
    SlackWorkspaceLink,
)
from apps.slack.verify import sign
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user

SECRET = "test-signing-secret"


TEAM, BOT, ALICE, BOB = "T1", "UBOT", "UALICE", "UBOB"


@pytest.fixture
def slack_configured(settings):
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
            # `team_id` as real users.info returns it: both are full members of our Slack.
            ALICE: {"name": "alice", "team_id": TEAM,
                    "profile": {"email": "alice@dimagi.com", "real_name": "Alice A"}},
            BOB: {"name": "bob", "team_id": TEAM, "profile": {"email": "bob@dimagi.com", "real_name": "Bob B"}},
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
        # Timestamps chat.postMessage hands out, in order, before falling back to one fixed ts.
        self.ts_seq: list[str] = []
        # channel id -> name, for conversations.info. Empty = answer like an
        # install without channels:read (`{"ok": true}` and no channel).
        self.channels: dict[str, str] = {}
        # When set, chat.getPermalink answers like Slack does for this workspace domain.
        self.permalink_base = ""
        # What an install grants (oauth.v2.access `scope`); None = everything canopy asks for.
        self.granted: list[str] | None = None
        # What auth.test's X-OAuth-Scopes header says; None = no header.
        self.header_scopes: list[str] | None = None
        # channel id -> who is in it, for conversations.members. A channel not here
        # answers like Slack with no list (`{"ok": true}`), which canopy reads as unknown.
        self.members: dict[str, list[str]] = {}

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
            ts = self.ts_seq.pop(0) if self.ts_seq else "1700000999.000100"
            body = {"ok": True, "ts": ts, "channel": payload.get("channel")}
        elif method == "conversations.info" and payload.get("channel") in self.channels:
            body = {"ok": True, "channel": {"id": payload["channel"],
                                            "name": self.channels[payload["channel"]]}}
        elif method == "chat.getPermalink" and self.permalink_base:
            ts = str(payload.get("message_ts") or "").replace(".", "")
            body = {"ok": True, "permalink": f"{self.permalink_base}/archives/{payload.get('channel')}/p{ts}"}
        elif method == "conversations.members" and payload.get("channel") in self.members:
            body = {"ok": True, "members": list(self.members[payload["channel"]])}
        elif method == "users.list":
            body = {"ok": True, "members": [{"id": uid, **info} for uid, info in self.users.items()]}
        elif method == "users.lookupByEmail":
            uid = self.lookup.get(payload.get("email", ""))
            body = {"ok": True, "user": {"id": uid}} if uid else {"ok": False, "error": "users_not_found"}
        elif method == "oauth.v2.access":
            from apps.slack.views_auth import BOT_SCOPES
            body = {"ok": True, "access_token": "xoxb-new", "bot_user_id": BOT, "app_id": "A_CANOPY",
                    "scope": ",".join(BOT_SCOPES if self.granted is None else self.granted),
                    "team": {"id": TEAM, "name": "Dimagi"}, "authed_user": {"id": self.installer}}
        elif method == "auth.test":
            body = {"ok": True}
            if self.header_scopes is not None:
                resp.headers = {"X-OAuth-Scopes": ",".join(self.header_scopes)}
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
    inst = SlackInstallation(team_id=TEAM, team_name="Dimagi", bot_user_id=BOT)
    inst.bot_token = "xoxb-test"
    inst.save()
    SlackWorkspaceLink.objects.create(installation=inst, workspace=ws)
    return inst


@pytest.fixture
def alice(ws):
    from allauth.account.models import EmailAddress

    user = a_user("alice@dimagi.com")
    # Signed in with Google, as a real member has: the address is a verified one.
    EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
    wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    return user


@pytest.fixture
def linked(installation, alice):
    return SlackUserLink.objects.create(installation=installation, slack_user_id=ALICE, user=alice)


@pytest.fixture
def hal(ws):
    return Agent.objects.create(slug="hal", name="Hal", workspace=ws, slack_enabled=True)


def _post(path: str, body: bytes, content_type: str, *, secret: str = SECRET, req_ts: str | None = None):
    """One Slack request, as production runs it — INCLUDING its commit.

    The status line is posted by the `turn_status_changed` receiver, which
    fires post-commit. This file runs each test inside a transaction that never
    commits, so without executing the callbacks here every status-line
    assertion would pass or fail on something production never does.
    Production has no ATOMIC_REQUESTS, so there the callback runs inside the
    request that enqueued the turn — which is what this reproduces.
    """
    from django.test import TestCase

    ts = req_ts or str(int(time.time()))
    with TestCase.captureOnCommitCallbacks(execute=True):
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


def _link_client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _owner(ws):
    user = a_user("owner@dimagi.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.OWNER)
    return user


def thread_reply(text: str, *, user=ALICE, ts="1700000050.000100", thread_ts="1700000000.000100"):
    return event({"type": "message", "channel_type": "channel", "user": user, "text": text,
                  "ts": ts, "thread_ts": thread_ts, "channel": "C1"})


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


def _commands(slack) -> dict[str, dict]:
    return {c["command"]: c for c in slack.manifest["features"]["slash_commands"]}


@pytest.fixture
def owner_client(ws):
    return _link_client(_owner(ws))
