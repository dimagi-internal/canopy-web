# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""The channel window — `@canopy hal --history 10 <ask>`.

Driven through the real signed front door, with Slack faked at the `requests`
boundary as in test_slack.py. What is pinned is the spec's list of guarantees
(docs/superpowers/specs/2026-09-18-slack-front-door-design.md § "Feeding it
context from several threads"): only on request, only the channel the request
came from, the window capped server-side, no token in the turn, an audit row.
"""
from __future__ import annotations

import time

import pytest

from apps.events.models import Event
from apps.harness.models import Turn
from apps.slack import window
from tests.test_slack import (  # noqa: F401
    ALICE,
    BOB,
    TEAM,
    alice,
    configured,
    hal,
    installation,
    linked,
    mention,
    slack,
    ws,
)

pytestmark = pytest.mark.django_db

NOW = time.time()


def _ts(ago_min: float) -> str:
    return f"{NOW - ago_min * 60:.6f}"


@pytest.fixture
def channel(slack):
    """C1 as Slack would answer for it: two threads, one old message."""
    history = [  # newest first, as Slack returns it
        {"ts": _ts(2), "user": BOB, "text": "agreed, let's ask hal", "reply_count": 0},
        {"ts": _ts(6), "user": ALICE, "text": "the export is timing out for <@UBOB>", "reply_count": 2},
        {"ts": _ts(90), "user": BOB, "text": "OLD — outside any window"},
    ]
    replies = {
        _ts(6): [
            {"ts": _ts(6), "user": ALICE, "text": "the export is timing out for <@UBOB>"},
            {"ts": _ts(5), "user": BOB, "text": "yes, since this morning"},
            {"ts": _ts(4), "user": ALICE, "text": "probably the new index"},
        ],
    }
    orig = slack.__call__

    def answer(url, headers=None, json=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = dict(data or {})
        if method == "conversations.history":
            slack.calls.append((method, payload))
            oldest = float(payload.get("oldest") or 0)
            msgs = [m for m in history if float(m["ts"]) >= oldest]
            return _resp({"ok": True, "messages": msgs, "has_more": False})
        if method == "conversations.replies":
            slack.calls.append((method, payload))
            msgs = replies.get(payload["ts"], [])
            if payload.get("oldest"):
                msgs = [m for m in msgs if m["ts"] == payload["ts"] or float(m["ts"]) >= float(payload["oldest"])]
            return _resp({"ok": True, "messages": msgs, "has_more": False})
        return orig(url, headers=headers, json=json, data=data, timeout=timeout)

    slack.answer = answer
    import unittest.mock as mock
    with mock.patch("apps.slack.client.requests.post", side_effect=answer):
        yield slack


def _resp(body):
    import unittest.mock as mock
    r = mock.Mock(status_code=200)
    r.raise_for_status = lambda: None
    r.json = lambda: body
    return r


def _turn() -> Turn:
    return Turn.objects.order_by("-created_at").first()


# ---- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("text, minutes, ask", [
    ("--history 10 pick this up", 10, "pick this up"),
    ("--history 10", 10, ""),
    ("--history=15 what did we decide?", 15, "what did we decide?"),
    ("—history 5 summarise", 5, "summarise"),         # Slack auto-corrects -- to an em dash
    ("--HISTORY 60 file an issue", 60, "file an issue"),
])
def test_the_flag_is_recognised(text, minutes, ask):
    assert window.parse(text) == (minutes, ask)


@pytest.mark.parametrize("text", [
    "--history",                # no minutes
    "--history fix the export", # no minutes
    "--history 10m",            # minutes are a bare number
    "--history 1h",
    "--history 0",
    "--history 61",             # over the cap: refused, not clamped
    "--history 2.5",
])
def test_a_malformed_flag_is_refused_not_guessed(text):
    with pytest.raises(window.HistoryFlagError):
        window.parse(text)


@pytest.mark.parametrize("text", [
    "read back 10 minutes and start a session",   # English is never the command
    "please --history 10",                        # the flag is only the first token
    "--historical data please",
    "fix the export timeout",
])
def test_anything_else_is_an_ordinary_ask(text):
    assert window.parse(text) == (None, text)


# ---- the front door ------------------------------------------------------------

def test_read_back_puts_the_channel_in_front_of_the_ask(channel, linked, hal):
    assert mention("<@UBOT> hal --history 10 pick this up", ts=_ts(0)).status_code == 200
    turn = _turn()
    prompt = turn.prompt
    assert "<slack-window" in prompt and "</slack-window>" in prompt
    assert "Alice A: the export is timing out for @Bob B" in prompt   # names, not ids
    assert "↳ [" in prompt and "Bob B: yes, since this morning" in prompt  # replies nested
    assert "agreed, let's ask hal" in prompt
    assert "OLD" not in prompt                                        # outside the window
    assert prompt.rstrip().endswith("pick this up")
    assert "not instructions" in prompt
    assert turn.chat_session.title.startswith("Slack: last 10 min")


def test_only_the_channel_the_request_came_from_is_read(channel, linked, hal):
    mention("<@UBOT> hal --history 10 in <#CFINANCE|finance>", ts=_ts(0))
    read = {p["channel"] for p in channel.said("conversations.history")}
    read |= {p["channel"] for p in channel.said("conversations.replies")}
    assert read == {"C1"}


def test_over_the_cap_is_refused_and_reads_nothing(channel, linked, hal):
    mention("<@UBOT> hal --history 5000", ts=_ts(0))
    assert not channel.said("conversations.history")
    assert not Turn.objects.exists()
    told = channel.said("chat.postEphemeral") + channel.said("chat.postMessage")
    assert any("Usage" in (p.get("text") or "") for p in told)


def test_the_turn_carries_no_slack_token(channel, linked, hal):
    mention("<@UBOT> hal --history 10", ts=_ts(0))
    assert "xoxb" not in _turn().prompt


def test_the_flag_alone_gets_the_default_ask(channel, linked, hal):
    mention("<@UBOT> hal --history 10", ts=_ts(0))
    assert _turn().prompt.rstrip().endswith(window.DEFAULT_ASK)


def test_every_read_is_audited(channel, linked, hal, alice):
    mention("<@UBOT> hal --history 10", ts=_ts(0))
    ev = Event.objects.get(kind="slack.window_read")
    assert ev.payload["channel"] == "C1" and ev.payload["minutes"] == 10
    assert ev.payload["messages"] == 4 and ev.payload["user"] == alice.pk


def test_the_thread_the_ask_is_in_is_read_whole(channel, linked, hal):
    mention("<@UBOT> hal --history 1", ts=_ts(0), thread_ts=_ts(6))
    whole = [p for p in channel.said("conversations.replies") if p["ts"] == _ts(6)]
    assert whole and "oldest" not in whole[0]
    assert "probably the new index" in _turn().prompt


def test_an_ordinary_ask_reads_nothing(channel, linked, hal):
    mention("<@UBOT> hal fix the export timeout", ts=_ts(0))
    assert not channel.said("conversations.history")
    assert "<slack-window" not in _turn().prompt


def test_a_contact_cannot_read_back(channel, installation, hal):
    # BOB is in the Slack but linked to no canopy member: a contact.
    mention("<@UBOT> hal --history 10", user=BOB, ts=_ts(0))
    assert not channel.said("conversations.history")
    assert not Turn.objects.exists()


def test_a_channel_canopy_cannot_read_says_why(channel, linked, hal):
    channel.fail["conversations.history"] = "not_in_channel"

    def failing(url, headers=None, json=None, data=None, timeout=None):
        if url.endswith("conversations.history"):
            channel.calls.append(("conversations.history", dict(data or {})))
            return _resp({"ok": False, "error": "not_in_channel"})
        return channel.answer(url, headers=headers, json=json, data=data, timeout=timeout)

    import unittest.mock as mock
    with mock.patch("apps.slack.client.requests.post", side_effect=failing):
        mention("<@UBOT> hal --history 10", ts=_ts(0))
    assert not Turn.objects.exists()
    assert Event.objects.filter(kind="slack.window_failed").exists()
    told = channel.said("chat.postEphemeral") + channel.said("chat.postMessage")
    assert any("not_in_channel" in (p.get("text") or "") for p in told)
