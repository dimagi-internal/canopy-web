"""Sessions to post reply buttons from, and pressing them through the real signed door.
Shared by `tests/test_slack_choices.py` and its PostgreSQL sibling."""
from __future__ import annotations

import json
from urllib.parse import urlencode

import pytest

from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner, Turn
from tests.fixtures.slack import ALICE, TEAM, _post

POSTED_TS = "1700000999.000100"   # what FakeSlack answers chat.postMessage with
ACTIONS = [{"label": "Book 1: November goals", "reply": "book 1"},
           {"label": "Book all", "reply": "book all", "style": "primary"},
           {"label": "Skip today", "reply": "skip today"}]


@pytest.fixture
def repo_session(ws, alice):
    session = Session.objects.create(workspace=ws, project="canopy-web", origin=Session.ORIGIN_RUNNER, title="slack")
    box = Runner.objects.create(name="alice-mbp", kind=Runner.EMDASH, host="alice-mac", owner=alice)
    RunnerBinding.objects.create(session=session, runner=box, session_key="slack",
                                 emdash_project="canopy-web", transcript_id="claude-abc")
    return session


@pytest.fixture
def hal_session(ws, alice, hal):
    session = Session.objects.create(workspace=ws, agent=hal, origin=Session.ORIGIN_RUNNER, title="t", created_by=alice)
    box = Runner.objects.create(name="alice-hal-box", kind=Runner.EMDASH, host="alice-hal", owner=alice)
    RunnerBinding.objects.create(session=session, runner=box, session_key="t", emdash_project="hal",
                                 transcript_id="claude-hal")
    return session


def _buttons(post: dict) -> list[dict]:
    return [e for b in post.get("blocks", []) if b["type"] == "actions" for e in b["elements"]]


def _press(post: dict, index: int = 0, *, user=ALICE, thread_ts=POSTED_TS):
    """What Slack sends for a press: the message itself comes back in the payload."""
    button = _buttons(post)[index]
    payload = {"type": "block_actions", "team": {"id": TEAM}, "user": {"id": user},
               "channel": {"id": "C1"},
               "container": {"type": "message", "message_ts": POSTED_TS, "channel_id": "C1"},
               "message": {"ts": POSTED_TS, "thread_ts": thread_ts, "text": post["text"], "blocks": post["blocks"]},
               "actions": [{"action_id": button["action_id"], "value": button["value"]}],
               "state": {"values": {}}}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    response = _post("/api/slack/interactions", body, "application/x-www-form-urlencoded")
    assert response.status_code == 200
    return response


def _prompts(session) -> list[str]:
    return [t.prompt for t in Turn.objects.filter(chat_session=session).order_by("created_at")]
