"""The session list can carry each session's last agent reply — the supervisor feed.

The feed answers "which conversations finished and are waiting for my next
prompt, and what did the agent say?" without opening each one. Two things make
that possible on the LIST read:

- `last_reply`: the agent's most recent reply text, so the feed can render it.
- `agent_spoke_last`: the agent had the last word, i.e. it is the person's turn.

Both are opt-in (`?reply=true`): every other caller of the list — the chat home's
20s poll, embedded widgets — would otherwise pay for text it never shows.

A runner-discovered session holds no Message rows until someone opens it; its
recent history is the runner-reported `binding.tail`, refreshed on every report.
That is the source the feed reads for those sessions, or an emdash task that
finished while nobody was watching would show nothing.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    me = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=me)
    WorkspaceMembership.objects.create(user=me, workspace=ws, role=WorkspaceMembership.OWNER)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws)
    client = Client()
    client.force_login(me)
    return {"me": me, "ws": ws, "hal": hal, "client": client}


def _web_session(world, *rows):
    s = Session.objects.create(workspace_id=world["ws"].pk, created_by=world["me"],
                               agent=world["hal"], title="a chat")
    for i, (role, text) in enumerate(rows):
        Message.objects.create(session=s, turn_index=i, role=role, plaintext=text,
                               content={"text": text})
    return s


def _row(world, session, query="?reply=true"):
    rows = world["client"].get(f"/api/canopy-sessions/{query}").json()
    return next(r for r in rows if r["id"] == str(session.id))


def test_a_finished_web_chat_carries_its_last_reply(world):
    s = _web_session(
        world,
        (Message.USER, "fix the flaky test"),
        (Message.ASSISTANT, "Looking."),
        (Message.TOOL_USE, "Bash"),
        (Message.TOOL_RESULT, "ok"),
        (Message.ASSISTANT, "**Fixed** — it was a timezone bug. Ship it?"),
    )
    row = _row(world, s)
    # The LAST assistant row, tool rows skipped — not the first, not a tool result.
    assert row["last_reply"] == "**Fixed** — it was a timezone bug. Ship it?"
    assert row["agent_spoke_last"] is True


def test_once_the_person_has_replied_it_is_no_longer_their_turn(world):
    s = _web_session(
        world,
        (Message.USER, "fix it"),
        (Message.ASSISTANT, "Fixed. Ship it?"),
        (Message.USER, "yes"),
    )
    row = _row(world, s)
    assert row["agent_spoke_last"] is False
    # Not your turn, so not in the feed — and the reply is sent whole now, so the
    # list carries it only for sessions the feed will actually render.
    assert row["last_reply"] == ""


def test_the_reply_is_opt_in(world):
    s = _web_session(world, (Message.USER, "hi"), (Message.ASSISTANT, "hello"))
    row = _row(world, s, query="")
    assert row["last_reply"] == ""
    assert row["agent_spoke_last"] is False


def test_a_runner_session_reads_the_reported_tail(world):
    """An emdash task nobody has opened has no Message rows — only the tail its
    runner reports. That has to be enough for the feed."""
    runner = Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=world["me"], workspace=world["ws"],
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
    )
    resp = world["client"].post(
        f"/api/harness/runners/{runner.id}/sessions",
        {"sessions": [{
            "emdash_task": "flaky-test", "project": "canopy-web",
            "recent_messages": [
                {"role": "user", "text": "fix the flaky test"},
                {"role": "assistant", "text": "Done — PR #12 is green."},
            ],
        }]},
        content_type="application/json",
    )
    assert resp.status_code == 200
    rows = world["client"].get("/api/canopy-sessions/?reply=true").json()
    [row] = [r for r in rows if r["title"] == "flaky-test" or r["session_key"] == "flaky-test"]
    assert row["last_reply"] == "Done — PR #12 is green."
    assert row["agent_spoke_last"] is True


def test_a_long_reply_arrives_whole(world):
    """The feed used to get the first 2400 characters, so "Show more" on a long
    answer still stopped mid-sentence."""
    long = "## Summary\n\n" + "word " * 3000 + "\n\nThe last line."
    s = _web_session(world, (Message.USER, "q"), (Message.ASSISTANT, long))
    row = _row(world, s)
    assert row["last_reply"] == long.strip()
    assert row["last_reply"].endswith("The last line.")
