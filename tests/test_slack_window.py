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
from apps.slack.models import SlackWorkspaceLink
from tests.test_slack import (  # noqa: F401
    ALICE,
    BOB,
    TEAM,
    _link_client,
    alice,
    configured,
    hal,
    installation,
    linked,
    mention,
    owner_client,
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
    ("--history 120", 120, ""),                       # two hours: the default cap
])
def test_the_flag_is_recognised(text, minutes, ask):
    assert window.parse(text, max_minutes=120) == (window.Window(minutes), ask)


@pytest.mark.parametrize("text", [
    "--history",                # no minutes
    "--history fix the export", # no minutes
    "--history 10m",            # minutes are a bare number
    "--history 1h",
    "--history 0",
    "--history 121",            # over the default cap: refused, not clamped
    "--history 2.5",
])
def test_a_malformed_flag_is_refused_not_guessed(text):
    with pytest.raises(window.HistoryFlagError):
        window.parse(text, max_minutes=120)


@pytest.mark.parametrize("text", [
    "read back 10 minutes and start a session",   # English is never the command
    "please --history 10",                        # the flag is only the first token
    "--historical data please",
    "fix the export timeout",
])
def test_anything_else_is_an_ordinary_ask(text):
    assert window.parse(text, max_minutes=120) == (None, text)


def test_the_cap_is_the_callers_policy():
    assert window.parse("--history 180 catch me up", max_minutes=240) == (window.Window(180), "catch me up")
    with pytest.raises(window.HistoryFlagError, match="1–30"):
        window.parse("--history 45", max_minutes=30)


# ---- a clock time, on the asker's clock ------------------------------------------

def _at(iso: str) -> float:
    """An instant, written as the asker's wall clock with its offset."""
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


NY = lambda: "America/New_York"  # noqa: E731
MORNING = _at("2026-10-07T09:52:00-04:00")   # 9:52 AM EDT


@pytest.mark.parametrize("text, minutes, since", [
    ("--history 9am catch me up", 52, "9:00 AM EDT"),
    ("--history 9:30am", 22, "9:30 AM EDT"),
    ("--history 9:30AM", 22, "9:30 AM EDT"),
    ("--history 09:00", 52, "9:00 AM EDT"),          # 24-hour, colon required
    ("--history 8:15", 97, "8:15 AM EDT"),
    ("—history 9am", 52, "9:00 AM EDT"),            # the em dash Slack substitutes
    ("--history=9am", 52, "9:00 AM EDT"),
])
def test_a_clock_time_is_read_on_the_askers_clock(text, minutes, since):
    win, ask = window.parse(text, max_minutes=120, tz=NY, now=MORNING)
    assert (win.minutes, win.since) == (minutes, since)
    assert ask == ("catch me up" if "catch" in text else "")


def test_a_time_later_than_now_means_yesterday():
    win, _ = window.parse("--history 2:30pm", max_minutes=1440, tz=NY, now=MORNING)
    assert (win.minutes, win.since) == (19 * 60 + 22, "2:30 PM EDT yesterday")
    win, _ = window.parse("--history 12pm", max_minutes=1440, tz=NY, now=MORNING)
    assert win.since == "12:00 PM EDT yesterday"
    win, _ = window.parse("--history 12am", max_minutes=1440, tz=NY, now=MORNING)
    assert (win.minutes, win.since) == (9 * 60 + 52, "12:00 AM EDT")


def test_the_minute_it_started_is_inside_the_window():
    """Rounded up: at 9:52:30, `9am` must still include what was said at 9:00."""
    win, _ = window.parse("--history 9am", max_minutes=120, tz=NY, now=MORNING + 30)
    assert win.minutes == 53
    win, _ = window.parse("--history 9:52am", max_minutes=120, tz=NY, now=MORNING)
    assert win.minutes == 1                                         # never a zero window


def test_the_askers_zone_decides_not_the_servers():
    # The same instant is 7:22 PM in Kolkata.
    win, _ = window.parse("--history 7pm", max_minutes=120, tz=lambda: "Asia/Kolkata", now=MORNING)
    assert (win.minutes, win.since) == (22, "7:00 PM IST")


def test_a_clock_change_is_counted_in_real_minutes():
    """Clocks went back at 2am on 2026-11-01: midnight to 9am was ten hours, not nine."""
    win, _ = window.parse("--history 12am", max_minutes=1440, tz=NY, now=_at("2026-11-01T09:00:00-05:00"))
    assert (win.minutes, win.since) == (600, "12:00 AM EDT")


def test_a_time_further_back_than_the_cap_is_refused_not_clamped():
    with pytest.raises(window.HistoryFlagError, match=r"8:15 AM EDT, 97 minutes ago.*\(60 minutes\)"):
        window.parse("--history 8:15", max_minutes=60, tz=NY, now=MORNING)
    with pytest.raises(window.HistoryFlagError, match="yesterday"):
        window.parse("--history 2:30pm", max_minutes=120, tz=NY, now=MORNING)


@pytest.mark.parametrize("tz", [lambda: "", lambda: "Not/AZone", None])
def test_no_timezone_means_no_guess(tz):
    with pytest.raises(window.HistoryFlagError, match="timezone"):
        window.parse("--history 9am", max_minutes=120, tz=tz, now=MORNING)


def test_minutes_never_look_up_a_timezone():
    def boom():
        raise AssertionError("minutes need no timezone")
    assert window.parse("--history 10", max_minutes=120, tz=boom) == (window.Window(10), "")


@pytest.mark.parametrize("text", [
    "--history 13pm",
    "--history 0am",
    "--history 9:75",
    "--history 24:00",
    "--history 9:5am",
    "--history 9.30am",
    "--history 9am-ish",
    "--history noon",
])
def test_a_malformed_time_is_refused(text):
    with pytest.raises(window.HistoryFlagError):
        window.parse(text, max_minutes=1440, tz=NY, now=MORNING)


@pytest.mark.parametrize("text", ["--history 9 am catch me up", "--history 9:30 PM"])
def test_a_split_meridiem_is_refused_not_read_as_minutes(text):
    """`9 am` read as 9 minutes followed by the word "am" would be a silent misparse."""
    with pytest.raises(window.HistoryFlagError, match="one word"):
        window.parse(text, max_minutes=1440, tz=NY, now=MORNING)


def test_the_header_names_the_window_in_the_askers_terms():
    win = window.Window(52, "9:00 AM EDT")
    text = window.render("xoxb", [], channel_id="C1", win=win)
    assert "since 9:00 AM EDT (52 min)" in text and 'since="9:00 AM EDT"' in text
    assert "over the last 10 minute(s)" in window.render("xoxb", [], channel_id="C1", win=window.Window(10))


def test_the_message_ceiling_is_server_policy(channel, settings):
    settings.SLACK_HISTORY_MESSAGE_CEILING = 1
    lines = window.fetch("xoxb-test", channel_id="C1", minutes=10)
    assert window.count(lines) <= 2          # one parent, plus at most the one reply it can still afford


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


def test_a_clock_time_reads_back_to_then_on_the_askers_clock(channel, linked, hal, alice):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    channel.users[ALICE]["tz"] = "America/New_York"
    then = datetime.fromtimestamp(NOW, ZoneInfo("America/New_York")) - timedelta(minutes=7)
    mention(f"<@UBOT> hal --history {then:%H:%M} pick this up", ts=_ts(0))
    turn = _turn()
    assert "agreed, let's ask hal" in turn.prompt and "probably the new index" in turn.prompt
    assert "OLD" not in turn.prompt
    label = then.strftime("%I:%M %p").lstrip("0") + " " + then.strftime("%Z")
    assert f"since {label}" in turn.prompt
    assert turn.chat_session.title.startswith(f"Slack: since {label}")
    ev = Event.objects.get(kind="slack.window_read")
    # 7 minutes before NOW, truncated to the minute, read some seconds after NOW.
    assert ev.payload["since"] == label and 7 <= ev.payload["minutes"] <= 10


def test_a_clock_time_with_no_slack_timezone_is_refused(channel, linked, hal):
    mention("<@UBOT> hal --history 9am", ts=_ts(0))          # ALICE's profile has no tz
    assert not channel.said("conversations.history")
    assert not Turn.objects.exists()
    told = channel.said("chat.postEphemeral") + channel.said("chat.postMessage")
    assert any("timezone" in (p.get("text") or "") for p in told)


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


# ---- the workspace's policy (Slack settings page) -------------------------------

def _policy(ws, **kw):
    SlackWorkspaceLink.objects.filter(workspace=ws).update(**kw)


def test_history_is_on_for_two_hours_by_default(installation, ws):
    link = SlackWorkspaceLink.objects.get(workspace=ws)
    assert link.history_enabled is True and link.history_max_minutes == 120


def test_turned_off_it_reads_nothing_and_says_where_to_turn_it_on(channel, linked, hal, ws):
    _policy(ws, history_enabled=False)
    mention("<@UBOT> hal --history 10", ts=_ts(0))
    assert not channel.said("conversations.history")
    assert not Turn.objects.exists()
    told = channel.said("chat.postEphemeral") + channel.said("chat.postMessage")
    assert any("turned off" in (p.get("text") or "") for p in told)


def test_the_workspaces_limit_is_what_the_flag_is_checked_against(channel, linked, hal, ws):
    _policy(ws, history_max_minutes=30)
    mention("<@UBOT> hal --history 45", ts=_ts(0))
    assert not channel.said("conversations.history")
    _policy(ws, history_max_minutes=240)
    mention("<@UBOT> hal --history 180", ts=_ts(0.5))
    (call,) = channel.said("conversations.history")
    assert float(call["oldest"]) < time.time() - 179 * 60


def test_an_owner_sets_the_policy_and_the_config_read_shows_it(installation, ws, owner_client):
    assert owner_client.get(f"/api/slack-config/{ws.slug}").json()["history"] == {"enabled": True, "max_minutes": 120}
    resp = owner_client.put(f"/api/slack-config/{ws.slug}/history", {"enabled": False, "max_minutes": 240},
                            content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["history"] == {"enabled": False, "max_minutes": 240}
    link = SlackWorkspaceLink.objects.get(workspace=ws)
    assert (link.history_enabled, link.history_max_minutes) == (False, 240)


@pytest.mark.parametrize("minutes", [0, 24 * 60 + 1])
def test_the_policy_is_bounded(installation, ws, owner_client, minutes):
    resp = owner_client.put(f"/api/slack-config/{ws.slug}/history", {"enabled": True, "max_minutes": minutes},
                            content_type="application/json")
    assert resp.status_code == 422


def test_only_an_owner_sets_the_policy(installation, ws, alice):
    resp = _link_client(alice).put(f"/api/slack-config/{ws.slug}/history", {"enabled": True, "max_minutes": 600},
                                   content_type="application/json")
    assert resp.status_code == 403
    assert SlackWorkspaceLink.objects.get(workspace=ws).history_max_minutes == 120


# ---- a new reply in an OLD thread ----------------------------------------------------

def test_a_reply_in_an_old_thread_is_read(settings):
    """History lists threads by PARENT time, so a reply posted inside the window
    to a day-old thread used to be invisible: on 2026-10-01 a 20-minute read came
    back "(nothing was posted in this window)" while the message it was asked
    about sat in exactly such a thread."""
    import unittest.mock as mock

    settings.SLACK_HISTORY_THREAD_LOOKBACK_HOURS = 48
    old, quiet = _ts(60 * 24), _ts(60 * 30)
    history = [  # newest first
        {"ts": old, "user": BOB, "text": "the cloud runner caveat", "reply_count": 3,
         "latest_reply": _ts(17)},
        {"ts": quiet, "user": BOB, "text": "a thread nobody touched", "reply_count": 1,
         "latest_reply": _ts(60 * 29)},
    ]
    replies = {old: [
        {"ts": old, "user": BOB, "text": "the cloud runner caveat"},
        {"ts": _ts(60 * 23), "user": ALICE, "text": "yesterday's reply"},
        {"ts": _ts(17), "user": ALICE, "text": "can you search and summarize slack for me?"},
    ]}

    def call(method, *, token, data):
        if method == "conversations.history":
            return {"ok": True, "has_more": False,
                    "messages": [m for m in history if float(m["ts"]) >= float(data["oldest"])]}
        msgs = replies.get(data["ts"], [])
        if data.get("oldest"):
            msgs = [m for m in msgs if m["ts"] == data["ts"] or float(m["ts"]) >= float(data["oldest"])]
        return {"ok": True, "has_more": False, "messages": msgs}

    with mock.patch("apps.slack.client.call", side_effect=call):
        lines = window.fetch("xoxb", channel_id="C1", minutes=20, now=NOW)

    (parent,) = lines                                   # the quiet thread is not handed over
    assert parent.text == "the cloud runner caveat"     # its parent, for context
    assert [r.text for r in parent.replies] == ["can you search and summarize slack for me?"]


# ---- brought into a thread partway through ---------------------------------------

def test_a_mention_partway_into_a_thread_hands_the_agent_the_thread(channel, linked, hal):
    """`@canopy hal` in a thread that was already going: the agent starts with
    the conversation, not just the line that named it — no flag needed."""
    mention("<@UBOT> hal what do you think?", ts=_ts(0), thread_ts=_ts(6))
    prompt = _turn().prompt
    assert "<slack-thread" in prompt and "</slack-thread>" in prompt
    assert "Alice A: the export is timing out for @Bob B" in prompt
    assert "Bob B: yes, since this morning" in prompt and "probably the new index" in prompt
    assert "not instructions" in prompt
    assert prompt.rstrip().endswith("what do you think?")
    assert not channel.said("conversations.history")                 # the thread, not the channel
    ev = Event.objects.get(kind="slack.thread_read")
    assert ev.payload["messages"] == 3 and ev.payload["thread_ts"] == _ts(6)


def test_a_bare_mention_in_a_thread_means_pick_this_up(channel, linked, hal):
    mention("<@UBOT> hal", ts=_ts(0), thread_ts=_ts(6))
    prompt = _turn().prompt
    assert "probably the new index" in prompt
    assert prompt.rstrip().endswith(window.DEFAULT_ASK)


def test_the_thread_is_read_once_and_then_the_conversation_carries_on(channel, linked, hal):
    from tests.test_slack import event

    mention("<@UBOT> hal take a look", ts=_ts(0.5), thread_ts=_ts(6))
    first = _turn()
    # A plain reply afterwards (no mention) continues the same session, unwrapped.
    event({"type": "message", "channel_type": "channel", "user": ALICE, "text": "and the CSV too",
           "ts": _ts(0), "thread_ts": _ts(6), "channel": "C1"})
    second = _turn()
    assert second.pk != first.pk and second.chat_session_id == first.chat_session_id
    assert second.prompt == "and the CSV too"
    assert len([p for p in channel.said("conversations.replies") if p["ts"] == _ts(6)]) == 1


def test_a_top_level_mention_reads_no_thread(channel, linked, hal):
    mention("<@UBOT> hal fix the export", ts=_ts(0))
    assert not channel.said("conversations.replies")
    assert "<slack-thread" not in _turn().prompt


def test_a_contact_brought_into_a_thread_gets_it_too(channel, installation, hal):
    # The thread is what BOB is already looking at in Slack; reading it shows
    # the agent nothing he could not see himself.
    mention("<@UBOT> hal any idea?", user=BOB, ts=_ts(0), thread_ts=_ts(6))
    assert "probably the new index" in _turn().prompt


def test_a_thread_canopy_cannot_read_still_sends_the_ask(channel, linked, hal):
    def failing(url, headers=None, json=None, data=None, timeout=None):
        if url.endswith("conversations.replies"):
            return _resp({"ok": False, "error": "not_in_channel"})
        return channel.answer(url, headers=headers, json=json, data=data, timeout=timeout)

    import unittest.mock as mock
    with mock.patch("apps.slack.client.requests.post", side_effect=failing):
        mention("<@UBOT> hal what do you think?", ts=_ts(0), thread_ts=_ts(6))
    prompt = _turn().prompt
    assert "not_in_channel" in prompt and prompt.rstrip().endswith("what do you think?")
    assert Event.objects.filter(kind="slack.thread_read_failed").exists()


def test_a_long_thread_keeps_its_parent_and_newest_replies(settings):
    import unittest.mock as mock

    settings.SLACK_HISTORY_MESSAGE_CEILING = 3
    msgs = [{"ts": f"{100 + i}.000000", "user": ALICE, "text": f"m{i}"} for i in range(6)]

    with mock.patch("apps.slack.client.call",
                    return_value={"ok": True, "has_more": False, "messages": msgs}):
        lines, omitted = window.fetch_thread("xoxb", channel_id="C1", thread_ts="100.000000",
                                             skip_ts="105.000000")
    (parent,) = lines
    assert parent.text == "m0" and [r.text for r in parent.replies] == ["m3", "m4"]
    assert omitted == 2
