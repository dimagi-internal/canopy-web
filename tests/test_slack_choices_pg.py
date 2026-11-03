# ruff: noqa: F811 — the fixtures below are imported from test_slack and then requested by name.
"""Two presses of one post, at once, against real row locks.

SQLite has none, so `tests/test_slack_choices.py` can only replay the interleavings.
Skipped there; CI's `Backend pytest (PostgreSQL)` job runs this.
"""
from __future__ import annotations

import threading
import time

import pytest
from django.db import connection, connections

from apps.slack import client, share
from tests.fixtures.slack import ALICE
from tests.fixtures.slack_choices import ACTIONS, POSTED_TS, _press, _prompts, repo_session  # noqa: F401

pytestmark = [
    pytest.mark.skipif(connection.vendor != "postgresql", reason="row locks need PostgreSQL"),
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("slack_configured"),
]


def _in_thread(fn, failures):
    def run():
        try:
            fn()
        except BaseException as e:  # noqa: BLE001 — carried back to the test's own thread
            failures.append(e)
        finally:
            connections.close_all()
    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_a_slow_rewrite_cannot_land_after_and_overwrite_a_newer_one(slack, linked, alice, repo_session, monkeypatch):
    """A's rewrite is on its way to Slack, slowly, when B is pressed. Unless B waits
    for it, B's complete rewrite reaches Slack first and A's older one lands on top."""
    result = share.share_session(alice, channel="C1", summary="Your day", mode=share.BIND,
                                 session=repo_session, actions=ACTIONS)
    assert result.ts == POSTED_TS
    post = slack.said("chat.postMessage")[0]

    a_is_drawing = threading.Event()
    landed: list[dict] = []
    real = client.update_message

    def slow_update(token, *, channel, ts, text, blocks=None):
        first = not a_is_drawing.is_set()
        a_is_drawing.set()
        if first:
            time.sleep(1)
        landed.append({"text": text, "blocks": blocks})
        real(token, channel=channel, ts=ts, text=text, blocks=blocks)

    monkeypatch.setattr(client, "update_message", slow_update)
    failures: list[BaseException] = []
    a = _in_thread(lambda: _press(post, 0), failures)
    assert a_is_drawing.wait(30)
    b = _in_thread(lambda: _press(post, 2), failures)
    a.join(30)
    b.join(30)
    assert not failures, failures
    assert sorted(_prompts(repo_session)) == ["book 1", "skip today"]
    assert len(landed) == 2
    last = landed[-1]["blocks"]
    assert [b["elements"][0]["text"] for b in last if b["type"] == "context"] == [
        f":white_check_mark: <@{ALICE}> chose: *book 1*", f":white_check_mark: <@{ALICE}> chose: *skip today*"]
    assert [e["text"]["text"] for b in last if b["type"] == "actions" for e in b["elements"]] == ["Book all"]


def test_two_presses_of_one_button_at_once_deliver_one_message(slack, linked, alice, repo_session):
    share.share_session(alice, channel="C1", summary="Your day", mode=share.BIND,
                        session=repo_session, actions=ACTIONS)
    post = slack.said("chat.postMessage")[0]
    failures: list[BaseException] = []
    threads = [_in_thread(lambda: _press(post, 1), failures) for _ in range(4)]
    for thread in threads:
        thread.join(30)
    assert not failures, failures
    assert _prompts(repo_session) == ["book all"]
