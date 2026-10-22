"""A Slack channel whose history can be read (conversations.history/replies/members faked
over tests.fixtures.slack's FakeSlack), and the ingest tick helpers. Registered as a
pytest plugin (root conftest.py).
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.slack import ingest
from apps.slack.models import SlackChannelIngest
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_user
from tests.fixtures.slack import ALICE, BOB

CHAN, OWEN, UHAL, CREATOR = "C1", "UOWEN", "UHAL", "UCREATOR"


def _resp(body, status=200, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {})
    r.raise_for_status = lambda: None
    r.json = lambda: body
    return r


class Channel:
    """C1's history, as Slack pages it: newest first, two to a page."""

    def __init__(self):
        self.history = [  # newest first
            {"ts": "1700000500.000000", "user": BOB, "text": "last word"},
            {"ts": "1700000400.000000", "user": ALICE, "text": "a thread about <@UBOB>", "reply_count": 2,
             "latest_reply": "1700000460.000000"},
            {"ts": "1700000300.000000", "user": BOB, "text": "middle"},
            {"ts": "1700000100.000000", "user": ALICE, "text": "first ever message"},
        ]
        self.replies = {"1700000400.000000": [
            {"ts": "1700000400.000000", "user": ALICE, "text": "a thread about <@UBOB>"},
            {"ts": "1700000450.000000", "user": BOB, "text": "reply one", "thread_ts": "1700000400.000000"},
            {"ts": "1700000460.000000", "user": ALICE, "text": "reply two", "thread_ts": "1700000400.000000"},
        ]}
        self.members = [ALICE, OWEN, UHAL, CREATOR]
        self.creator = CREATOR
        self.rate_limit_on: set[str] = set()   # cursors that answer 429 once
        self.reads: list[tuple[str, dict]] = []

    def answer(self, fake):
        def call(url, headers=None, json=None, data=None, timeout=None):
            method = url.rsplit("/", 1)[-1]
            p = dict(data or {})
            if method == "conversations.history":
                self.reads.append((method, p))
                cursor = p.get("cursor", "")
                if cursor in self.rate_limit_on:
                    self.rate_limit_on.discard(cursor)
                    return _resp({"ok": False, "error": "ratelimited"}, 429, {"Retry-After": "7"})
                msgs = [m for m in self.history if float(m["ts"]) >= float(p.get("oldest") or 0)]
                start = int(cursor or 0)
                page = msgs[start:start + 2]
                nxt = str(start + 2) if start + 2 < len(msgs) else ""
                return _resp({"ok": True, "messages": page, "has_more": bool(nxt),
                              "response_metadata": {"next_cursor": nxt}})
            if method == "conversations.replies":
                self.reads.append((method, p))
                msgs = [m for m in self.replies.get(p["ts"], [])
                        if m["ts"] == p["ts"] or float(m["ts"]) >= float(p.get("oldest") or 0)]
                return _resp({"ok": True, "messages": msgs, "has_more": False})
            if method == "conversations.list":
                self.reads.append((method, p))
                return _resp({"ok": True, "channels": [{"id": "C9", "name": "random"},
                                                       {"id": CHAN, "name": "canopy-support"}]})
            if method == "conversations.members":
                return _resp({"ok": True, "members": self.members})
            if method == "conversations.info":
                return _resp({"ok": True, "channel": {"id": CHAN, "name": "canopy-support",
                                                      "creator": self.creator}})
            return fake(url, headers=headers, json=json, data=data, timeout=timeout)
        return call


@pytest.fixture
def channel(slack):
    ch = Channel()
    with mock.patch("apps.slack.client.requests.post", side_effect=ch.answer(slack)):
        yield ch


@pytest.fixture
def hal_login(hal, ws):
    user = a_user("hal@dimagi-ai.com")
    wsvc.ensure_member(ws, user, WorkspaceMembership.EDITOR)
    hal.user = user
    hal.save()
    return user


def _reads(channel):
    return [m for m, _ in channel.reads]


def _run(row):
    """Tick until the ingest stops being due (bounded)."""
    for _ in range(20):
        SlackChannelIngest.objects.filter(pk=row.pk, next_attempt_at__isnull=False).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1))
        ingest.tick()
        row.refresh_from_db()
        if row.status != SlackChannelIngest.READING:
            return row
    return row
