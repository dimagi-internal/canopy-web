# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""The channel window — `@canopy hal --history 10 <ask>`.

Driven through the real signed events webhook, with Slack faked at the `requests`
boundary as in test_slack.py. What is pinned is the spec's list of guarantees
(docs/superpowers/specs/2026-09-18-slack-front-door-design.md § "Feeding it
context from several threads"): only on request, only the channel the request
came from, the window capped server-side, no token in the turn, an audit row.
"""
from __future__ import annotations

import time

import pytest

from apps.canopy_sessions.models import Session
from apps.events.models import Event
from apps.harness.models import Turn
from apps.slack import window
from apps.slack.models import SlackWorkspaceLink
from tests.fixtures.slack import ALICE, BOB, _link_client, event, mention

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("slack_configured")]

NOW = time.time()


@pytest.fixture(autouse=True)
def _now():
    # The messages sit minutes before NOW and the window is read against the real
    # clock, so NOW must be when the test runs — not when xdist imported the module,
    # which on a busy CI box can be minutes earlier.
    global NOW
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


@pytest.mark.parametrize("text, last, ask", [
    ("--history --msgs 5 summarise", 5, "summarise"),
    ("--history --msgs 5", 5, ""),
    ("--history --msgs=3 what did we decide?", 3, "what did we decide?"),
    ("--history --messages 1", 1, ""),
    ("—history —msgs 4 pick this up", 4, "pick this up"),   # Slack's auto-corrected dashes
    ("--HISTORY --MSG 2 go", 2, "go"),
])
def test_a_message_count_is_recognised(text, last, ask):
    """The count is the newest messages however old they are, so it carries no
    minutes: the workspace's limit is on how far back a TIME window may reach."""
    assert window.parse(text, max_minutes=120) == (window.Window(0, last=last), ask)


@pytest.mark.parametrize("text, minutes, ask", [
    ("--history --min 30 pick this up", 30, "pick this up"),
    ("--history --mins 10", 10, ""),
    ("--history --minutes=15 what did we decide?", 15, "what did we decide?"),
    ("—history —min 5 summarise", 5, "summarise"),
])
def test_minutes_can_name_their_unit(text, minutes, ask):
    assert window.parse(text, max_minutes=120) == (window.Window(minutes), ask)


@pytest.mark.parametrize("text", [
    "--history --msgs",            # no number
    "--history --msgs five",
    "--history --msgs 0",
    "--history --msgs 99999",      # over the server's message ceiling
    "--history --min 121",         # over the workspace's limit
    "--history --min 0",
    "--history --hours 2",         # not a unit
])
def test_a_unit_without_a_good_number_is_refused(text):
    with pytest.raises(window.HistoryFlagError):
        window.parse(text, max_minutes=120)


@pytest.mark.parametrize("text", ["--history 5msgs summarise", "--history 5 messages summarise"])
def test_a_count_with_the_unit_after_says_put_it_first(text):
    """Never read as 5 minutes and an ask starting "messages"."""
    with pytest.raises(window.HistoryFlagError, match="--history --msgs 5"):
        window.parse(text, max_minutes=120)


def _line(ts: float, *replies: float) -> window.Line:
    return window.Line(f"{ts:.6f}", "U", f"m{ts:g}", [window.Line(f"{r:.6f}", "U", f"m{r:g}") for r in replies])


def test_the_last_n_messages_are_the_newest_posted_wherever_they_were_posted():
    """A thread's replies count as messages; a reply whose parent is too old
    to keep stands alone, and a kept parent keeps only its kept replies."""
    lines = [_line(1, 2, 7), _line(3), _line(4, 5, 6), _line(8)]
    kept = window._newest(lines, 4)          # 5, 6, 7, 8
    assert [(ln.ts[:1], [r.ts[:1] for r in ln.replies]) for ln in kept] == [
        ("5", []), ("6", []), ("7", []), ("8", [])]
    kept = window._newest(lines, 6)          # 3..8: parent 4 is kept with both replies
    assert [(ln.ts[:1], [r.ts[:1] for r in ln.replies]) for ln in kept] == [
        ("3", []), ("4", ["5", "6"]), ("7", []), ("8", [])]
    assert window.count(window._newest(lines, 99)) == 8


def test_the_header_names_a_message_count():
    text = window.render("xoxb", [], channel_id="C1", win=window.Window(0, last=5))
    assert "the last 5 message(s)" in text and 'last="5"' in text
    assert "minutes=" not in text                     # a count has no time limit to report
    assert "in this window" not in text               # an empty read is an empty channel


def _old_channel(slack, history, replies=None):
    """A Slack answering for `history` (newest first), every message of it old."""
    import unittest.mock as mock

    def answer(url, headers=None, json=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = dict(data or {})
        if method == "conversations.history":
            slack.calls.append((method, payload))
            oldest = float(payload.get("oldest") or 0)
            return _resp({"ok": True, "has_more": False,
                          "messages": [m for m in history if float(m["ts"]) >= oldest]})
        if method == "conversations.replies":
            slack.calls.append((method, payload))
            oldest = float(payload.get("oldest") or 0)
            msgs = [m for m in (replies or {}).get(payload["ts"], [])
                    if m["ts"] == payload["ts"] or float(m["ts"]) >= oldest]
            return _resp({"ok": True, "has_more": False, "messages": msgs})
        return slack(url, headers=headers, json=json, data=data, timeout=timeout)

    return mock.patch("apps.slack.client.requests.post", side_effect=answer)


DAY = 24 * 60


def test_a_message_count_reaches_past_the_workspace_limit(channel, linked, hal):
    """The ask that started this: `--history --msgs 2` in a channel whose last two
    messages are hours old handed the agent nothing, and looked like a quiet channel."""
    history = [{"ts": _ts(3 * DAY), "user": BOB, "text": "NEWEST, three days old"},
               {"ts": _ts(4 * DAY), "user": ALICE, "text": "NEXT, four days old"},
               {"ts": _ts(5 * DAY), "user": BOB, "text": "OLDER"}]
    with _old_channel(channel, history):
        assert mention("<@UBOT> hal --history --msgs 2 pick this up", ts=_ts(0)).status_code == 200
    prompt = _turn().prompt
    assert "NEWEST, three days old" in prompt and "NEXT, four days old" in prompt
    assert "OLDER" not in prompt and 'messages="2"' in prompt


def test_a_count_reads_past_the_thread_lookback(channel, settings):
    settings.SLACK_HISTORY_THREAD_LOOKBACK_HOURS = 1
    history = [{"ts": _ts(30 * DAY), "user": BOB, "text": "a month old"}]
    with _old_channel(channel, history):
        lines = window.fetch("xoxb-test", channel_id="C1", last=1)
    assert [ln.text for ln in lines] == ["a month old"]
    assert not channel.calls[0][1].get("oldest")      # the scan names no start


def test_a_count_finds_a_recent_reply_in_an_old_thread(channel):
    history = [{"ts": _ts(2 * DAY), "user": BOB, "text": "newest parent"},
               {"ts": _ts(9 * DAY), "user": ALICE, "text": "old parent", "reply_count": 2,
                "latest_reply": _ts(DAY)}]
    replies = {_ts(9 * DAY): [{"ts": _ts(9 * DAY), "user": ALICE, "text": "old parent"},
                              {"ts": _ts(8 * DAY), "user": BOB, "text": "old reply"},
                              {"ts": _ts(DAY), "user": BOB, "text": "new reply"}]}
    with _old_channel(channel, history, replies):
        lines = window.fetch("xoxb-test", channel_id="C1", last=2)
    assert [ln.text for ln in lines] == ["newest parent", "new reply"]


def test_a_count_does_not_spend_itself_on_the_ask(channel):
    """The mention is a message in the channel, and the agent gets it as its prompt."""
    history = [{"ts": _ts(0), "user": ALICE, "text": "the ask"},
               {"ts": _ts(DAY), "user": BOB, "text": "said before it"},
               {"ts": _ts(2 * DAY), "user": BOB, "text": "OLDER"}]
    with _old_channel(channel, history):
        lines = window.fetch("xoxb-test", channel_id="C1", last=1, skip_ts=_ts(0))
    assert [ln.text for ln in lines] == ["said before it"]


def test_a_count_asks_slack_only_for_threads_it_could_keep(channel):
    history = [{"ts": _ts(DAY), "user": BOB, "text": "newest"},
               {"ts": _ts(9 * DAY), "user": ALICE, "text": "stale thread", "reply_count": 1,
                "latest_reply": _ts(8 * DAY)}]
    with _old_channel(channel, history):
        window.fetch("xoxb-test", channel_id="C1", last=1)
    assert [c for c in channel.calls if c[0] == "conversations.replies"] == []


def test_a_count_returns_what_a_short_channel_has(channel):
    history = [{"ts": _ts(40 * DAY), "user": BOB, "text": "all there is"}]
    with _old_channel(channel, history):
        lines = window.fetch("xoxb-test", channel_id="C1", last=5)
    assert [ln.text for ln in lines] == ["all there is"]


def test_a_time_window_still_stops_at_its_minutes(channel):
    """Only a count ignores the time limit."""
    lines = window.fetch("xoxb-test", channel_id="C1", minutes=10)
    assert "OLD" not in " ".join(ln.text for ln in lines)
    with pytest.raises(window.HistoryFlagError):
        window.parse("--history --min 121 go", max_minutes=120)


def test_read_back_by_message_count(channel, linked, hal):
    """The channel above holds four messages in its window (2, 6, 5 and 4
    minutes ago, the last three one thread); the newest two are 4 and 2."""
    assert mention("<@UBOT> hal --history --msgs 2 pick this up", ts=_ts(0)).status_code == 200
    turn = _turn()
    assert "agreed, let's ask hal" in turn.prompt and "probably the new index" in turn.prompt
    assert "yes, since this morning" not in turn.prompt and "OLD" not in turn.prompt
    assert 'last="2"' in turn.prompt and 'messages="2"' in turn.prompt
    assert turn.chat_session.title.startswith("Slack: last 2 messages")
    ev = Event.objects.get(kind="slack.window_read")
    assert ev.payload["last"] == 2 and ev.payload["messages"] == 2


def test_the_message_ceiling_is_server_policy(channel, settings):
    settings.SLACK_HISTORY_MESSAGE_CEILING = 1
    lines = window.fetch("xoxb-test", channel_id="C1", minutes=10)
    assert window.count(lines) <= 2          # one parent, plus at most the one reply it can still afford


# ---- the events webhook ------------------------------------------------------------

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
    # Seven minutes before a run just after midnight in New York is yesterday
    # there, and the label says so (CI failed on exactly this at 00:07 EDT).
    if then.date() != datetime.fromtimestamp(NOW, ZoneInfo("America/New_York")).date():
        label += " yesterday"
    assert f"since {label}" in turn.prompt
    assert turn.chat_session.title.startswith(f"Slack: since {label}")
    ev = Event.objects.get(kind="slack.window_read")
    # 7 minutes before NOW, truncated to the minute, read however long after
    # NOW this test runs: NOW is set at import, and a slow CI run reached this
    # test four minutes later (11 minutes against a fixed cap of 10).
    elapsed = int((time.time() - NOW) // 60)
    assert ev.payload["since"] == label and 7 <= ev.payload["minutes"] <= 9 + elapsed


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
    from tests.fixtures.slack import event

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


# --- `--history --threads N`: threads are the unit, each handed over whole.


@pytest.mark.parametrize("text,n,ask", [
    ("--history --threads 3 summarise", 3, "summarise"),
    ("--history --thread 1", 1, ""),
    ("--history --threads=2 what did we decide?", 2, "what did we decide?"),
    ("—history —threads 4 pick this up", 4, "pick this up"),
])
def test_a_thread_count_parses(text, n, ask):
    assert window.parse(text, max_minutes=120) == (window.Window(0, threads=n), ask)


@pytest.mark.parametrize("text", ["--history --threads", "--history --threads three", "--history --threads 0",
                                  f"--history --threads {window.THREAD_COUNT_CEILING + 1}"])
def test_a_bad_thread_count_is_refused(text):
    with pytest.raises(window.HistoryFlagError):
        window.parse(text, max_minutes=120)


@pytest.mark.parametrize("text", ["--history 3threads summarise", "--history 3 threads summarise"])
def test_a_thread_count_with_the_unit_after_is_refused_not_read_as_minutes(text):
    with pytest.raises(window.HistoryFlagError, match="--history --threads 3"):
        window.parse(text, max_minutes=120)


def test_threads_hand_over_each_chosen_thread_whole_however_old(channel, linked, hal):
    """Counted as threads, a busy thread is one thing and keeps its parent: the
    failure `--msgs` has, where the newest replies push the parent out."""
    a, b, c = _ts(5 * DAY), _ts(3 * DAY), _ts(4 * DAY)
    history = [{"ts": b, "user": BOB, "text": "STANDALONE"},
               {"ts": _ts(3.5 * DAY), "subtype": "channel_join", "user": BOB, "text": "joined"},
               {"ts": c, "user": BOB, "text": "QUIET THREAD", "reply_count": 1, "latest_reply": c},
               {"ts": a, "user": ALICE, "text": "NAZIER'S LONG MESSAGE", "reply_count": 2,
                "latest_reply": _ts(2 * DAY)}]
    replies = {a: [{"ts": a, "user": ALICE, "text": "NAZIER'S LONG MESSAGE"},
                   {"ts": _ts(4.5 * DAY), "user": BOB, "text": "FIRST REPLY"},
                   {"ts": _ts(2 * DAY), "user": BOB, "text": "LAST REPLY"}]}
    with _old_channel(channel, history, replies):
        assert mention("<@UBOT> hal --history --threads 2 pick this up", ts=_ts(0)).status_code == 200
    turn = _turn()
    assert "NAZIER'S LONG MESSAGE" in turn.prompt and "FIRST REPLY" in turn.prompt and "LAST REPLY" in turn.prompt
    assert "STANDALONE" in turn.prompt
    assert "QUIET THREAD" not in turn.prompt and "joined" not in turn.prompt
    assert 'threads="2"' in turn.prompt and 'messages="4"' in turn.prompt
    assert Session.objects.filter(title="Slack: last 2 threads — pick this up").exists()


def test_threads_past_the_ceiling_keep_each_parent_and_say_what_they_left_out(channel, linked, hal, settings):
    settings.SLACK_HISTORY_MESSAGE_CEILING = 3
    a = _ts(9)
    history = [{"ts": a, "user": ALICE, "text": "PARENT", "reply_count": 4, "latest_reply": _ts(1)}]
    replies = {a: [{"ts": a, "user": ALICE, "text": "PARENT"},
                   *[{"ts": _ts(5 - i), "user": BOB, "text": f"REPLY{i}"} for i in range(4)]]}
    with _old_channel(channel, history, replies):
        lines = window.fetch("xoxb", channel_id="C1", threads=1)
    assert lines[0].text == "PARENT" and [r.text for r in lines[0].replies] == ["REPLY2", "REPLY3"]
    assert lines[0].omitted == 2


# --- A thread link typed in the ask.

THREAD = "1793884682.216459"
LINK = "https://dimagi.slack.com/archives/C2OTHER/p1793884682216459"


@pytest.mark.parametrize("text,channel_id,ts", [
    (f"<{LINK}>", "C2OTHER", THREAD),
    (f"see <{LINK}|this thread> please", "C2OTHER", THREAD),
    (LINK, "C2OTHER", THREAD),                                     # a slash command's text is unwrapped
    (f"<{LINK}?thread_ts=1793884000.000100&amp;cid=C2OTHER>", "C2OTHER", "1793884000.000100"),  # a reply's link
    (f"<{LINK}?thread_ts=1793884000.000100&cid=C2OTHER>", "C2OTHER", "1793884000.000100"),
])
def test_a_thread_link_is_understood(text, channel_id, ts):
    assert [(ln.channel_id, ln.thread_ts) for ln in window.thread_links(text)] == [(channel_id, ts)]


@pytest.mark.parametrize("text", ["https://dimagi.slack.com/archives/C2OTHER", "https://example.com/archives/C2OTHER/p1793884682216459"])
def test_what_is_not_a_thread_link_is_left_alone(text):
    assert window.thread_links(text) == []


def test_the_same_thread_linked_twice_is_one_link():
    assert len(window.thread_links(f"<{LINK}> and again <{LINK}|here>")) == 1


def test_the_ask_without_its_links():
    assert window.without_links(f"<{LINK}|this thread>") == ""
    assert window.without_links(f"summarise <{LINK}> for me") == "summarise for me"


def _linked_channel(slack, *, members=(ALICE,), info=None, replies=None, failing=""):
    """A Slack answering for the linked channel C2OTHER, one the asker may or may not be in."""
    import unittest.mock as mock

    def answer(url, headers=None, json=None, data=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        payload = dict(data or {})
        if method == failing:
            return _resp({"ok": False, "error": "not_in_channel"})
        if method == "conversations.members":
            return _resp({"ok": True, "members": list(members)})
        if method == "conversations.info":
            return _resp({"ok": True, "channel": {"id": payload["channel"], **(info or {})}})
        if method == "conversations.replies":
            slack.calls.append((method, payload))
            return _resp({"ok": True, "has_more": False, "messages": replies if replies is not None else [
                {"ts": THREAD, "user": BOB, "text": "THE LINKED THREAD"},
                {"ts": "1793884690.000100", "user": ALICE, "text": "ITS REPLY"}]})
        return slack(url, headers=headers, json=json, data=data, timeout=timeout)

    return mock.patch("apps.slack.client.requests.post", side_effect=answer)


def _read_replies(slack):
    return [c for c in slack.calls if c[0] == "conversations.replies"]


def test_a_link_alone_hands_over_the_thread_and_picks_it_up(channel, linked, hal):
    with _linked_channel(channel):
        assert mention(f"<@UBOT> hal <{LINK}>", ts=_ts(0)).status_code == 200
    turn = _turn()
    assert "THE LINKED THREAD" in turn.prompt and "ITS REPLY" in turn.prompt
    assert 'channel="C2OTHER"' in turn.prompt and window.DEFAULT_ASK in turn.prompt
    assert "QUOTED MATERIAL" in turn.prompt
    assert Session.objects.filter(title="Slack: linked thread").exists()
    row = Event.objects.get(kind="slack.link_read")
    assert row.payload["channel"] == "C2OTHER" and row.payload["messages"] == 2


def test_a_link_with_an_ask_keeps_the_ask(channel, linked, hal):
    with _linked_channel(channel):
        mention(f"<@UBOT> hal summarise <{LINK}> for me", ts=_ts(0))
    assert "THE LINKED THREAD" in _turn().prompt and _turn().prompt.endswith(f"summarise <{LINK}> for me")


def test_a_link_in_a_channel_the_asker_is_not_in_is_not_read(channel, linked, hal):
    with _linked_channel(channel, members=(BOB,)):
        mention(f"<@UBOT> hal <{LINK}>", ts=_ts(0))
    turn = _turn()
    assert not _read_replies(channel) and "THE LINKED THREAD" not in turn.prompt
    assert "did not read it" in turn.prompt and "not a member" in turn.prompt
    assert Event.objects.filter(kind="slack.link_refused").exists()


def test_a_private_channels_thread_does_not_come_into_another_channel(channel, linked, hal):
    with _linked_channel(channel, info={"is_private": True}):
        mention(f"<@UBOT> hal <{LINK}>", ts=_ts(0))
    assert not _read_replies(channel) and "private" in _turn().prompt
    assert "THE LINKED THREAD" not in _turn().prompt


def test_a_private_channels_thread_is_read_in_a_dm_with_the_bot(channel, linked, hal):
    with _linked_channel(channel, info={"is_private": True}):
        event({"type": "message", "channel_type": "im", "user": ALICE, "text": f"hal <{LINK}>",
               "ts": _ts(0), "channel": "D1"})
    assert "THE LINKED THREAD" in _turn().prompt


def test_a_thread_in_the_same_channel_needs_no_membership_check(channel, linked, hal):
    same = "https://dimagi.slack.com/archives/C1/p1793884682216459"
    with _linked_channel(channel, members=(), failing="conversations.members"):
        mention(f"<@UBOT> hal <{same}>", ts=_ts(0))
    assert "THE LINKED THREAD" in _turn().prompt


def test_the_history_switch_governs_a_link(channel, linked, hal):
    SlackWorkspaceLink.objects.update(history_enabled=False)
    with _linked_channel(channel):
        mention(f"<@UBOT> hal <{LINK}>", ts=_ts(0))
    assert not _read_replies(channel) and "turned off" in _turn().prompt


def test_a_thread_slack_will_not_give_us_does_not_lose_the_ask(channel, linked, hal):
    with _linked_channel(channel, failing="conversations.replies"):
        mention(f"<@UBOT> hal summarise <{LINK}>", ts=_ts(0))
    prompt = _turn().prompt
    assert "not_in_channel" in prompt and prompt.endswith(f"summarise <{LINK}>")


def test_a_link_inside_quoted_history_is_never_followed(channel, linked, hal):
    """The injection this guards: someone posts a link in a channel, and a later
    `--history` would make canopy read a thread nobody asked for."""
    history = [{"ts": _ts(3), "user": BOB, "text": f"everyone read <{LINK}>"}]
    with _old_channel(channel, history):
        mention("<@UBOT> hal --history --msgs 1 summarise", ts=_ts(0))
    assert not _read_replies(channel)
    assert "THE LINKED THREAD" not in _turn().prompt and "did not read it" not in _turn().prompt


def test_past_the_link_ceiling_the_agent_is_told(channel, linked, hal):
    many = " ".join(f"<https://dimagi.slack.com/archives/C2OTHER/p17938846822164{i:02d}>"
                    for i in range(window.LINK_CEILING + 1))
    with _linked_channel(channel):
        mention(f"<@UBOT> hal {many}", ts=_ts(0))
    assert len(_read_replies(channel)) == window.LINK_CEILING
    assert f"at most {window.LINK_CEILING} linked threads" in _turn().prompt
