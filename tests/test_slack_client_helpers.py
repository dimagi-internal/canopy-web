"""The shared Slack read helpers: one pager, one way to name a person."""

from unittest import mock

from apps.slack import client


def test_display_name_prefers_the_full_name_then_falls_back():
    assert client.display_name({"profile": {"real_name": "Alice A", "display_name": "al"}}) == "Alice A"
    assert client.display_name({"profile": {"display_name": "al"}, "name": "alice"}) == "al"
    assert client.display_name({"name": "alice"}) == "alice"
    assert client.display_name({}, "U1") == "U1"


def test_user_name_falls_back_to_the_id_when_slack_will_not_say():
    with mock.patch.object(client, "user_info", side_effect=client.SlackApiError("users.info", "user_not_found")):
        assert client.user_name("t", "U9") == "U9"


def test_message_name_reads_a_bots_own_name():
    assert client.message_name({"username": "hal"}) == "hal"
    assert client.message_name({"bot_profile": {"name": "canopy"}}) == "canopy"
    assert client.message_name({"user": "U1"}) == ""


def test_paginate_follows_the_cursor_up_to_the_budget():
    pages = [
        {"messages": [{"ts": "1"}, {"ts": "2"}], "has_more": True, "response_metadata": {"next_cursor": "c2"}},
        {"messages": [{"ts": "3"}], "has_more": False, "response_metadata": {"next_cursor": "c3"}},
    ]
    with mock.patch.object(client, "call", side_effect=pages) as call:
        out = client.paginate("t", "conversations.history", {"channel": "C1"}, budget=10)
    assert [m["ts"] for m in out] == ["1", "2", "3"]
    assert call.call_args_list[1].kwargs["data"]["cursor"] == "c2"
    with mock.patch.object(client, "call", side_effect=pages):
        assert len(client.paginate("t", "conversations.history", {}, budget=1)) == 1


def test_page_reports_no_cursor_when_slack_has_no_more():
    body = {"messages": [], "has_more": False, "response_metadata": {"next_cursor": "x"}}
    with mock.patch.object(client, "call", return_value=body):
        assert client.page("t", "conversations.replies", {}, "c") == ([], "")
