"""The doorbell: push-triggered inbox checks, per-mailbox timers, and the
discovery tag that lets the poll audit the push path."""
import json
from types import SimpleNamespace

import pytest

from canopy_runner import inbox, inbox_due


@pytest.fixture(autouse=True)
def _clean():
    inbox_due.take_pending()
    inbox._seen_state.clear()
    yield
    inbox_due.take_pending()
    inbox._seen_state.clear()


class FakeClient:
    def __init__(self):
        self.enqueued = []

    def enqueue_turn(self, agent, origin, idem, *, prompt="", origin_ref=None,
                     routing="prefer_local"):
        self.enqueued.append({"idem": idem, "origin_ref": origin_ref})
        return {"id": "t-x", "_created": True}


def _runner(threads, calls=None):
    payload = json.dumps({"threads": threads})

    def run(cmd, capture_output, text, timeout):
        if calls is not None:
            calls.append(cmd)
        if "thread" in cmd and "get" in cmd:
            # A thread-get: answer with a sender that is NOT the mailbox.
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"messages": [{"payload": {"headers": [
                        {"name": "From", "value": "someone@else.com"}]}}]}
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    return run


MAILBOXES = {
    "eva": {"account": "eva@dimagi-ai.com", "client": "canopy"},
    "hal": {"account": "hal@dimagi-ai.com", "client": "canopy"},
}


# ── which mailboxes are due ──────────────────────────────────────────────────


def test_a_rung_mailbox_is_due_immediately():
    inbox_due.ring("eva@dimagi-ai.com")
    rung = inbox_due.take_pending()
    due = inbox_due.due(MAILBOXES, {"eva": 1000.0, "hal": 1000.0},
                        now=1001.0, interval=300, rung=rung)
    assert due == ["eva"]


def test_the_timer_still_fires_without_a_doorbell():
    due = inbox_due.due(MAILBOXES, {"eva": 0.0, "hal": 0.0},
                        now=400.0, interval=300, rung=set())
    assert set(due) == {"eva", "hal"}


def test_a_doorbell_for_one_mailbox_does_not_defer_another():
    """Per-mailbox stamps: a busy mailbox must not keep resetting a quiet one's
    timer, because the quiet ones are where a broken watch hides."""
    inbox_due.ring("eva@dimagi-ai.com")
    rung = inbox_due.take_pending()
    due = inbox_due.due(MAILBOXES, {"eva": 999.0, "hal": 0.0},
                        now=1000.0, interval=300, rung=rung)
    assert set(due) == {"eva", "hal"}  # eva rung, hal's own timer elapsed


def test_nothing_is_due_when_neither_clock_says_so():
    assert inbox_due.due(MAILBOXES, {"eva": 990.0, "hal": 990.0},
                         now=1000.0, interval=300, rung=set()) == []


def test_ringing_is_case_and_space_insensitive():
    inbox_due.ring("  EVA@Dimagi-AI.com ")
    rung = inbox_due.take_pending()
    assert inbox_due.due(MAILBOXES, {"eva": 1000.0, "hal": 1000.0}, now=1000.0,
                         interval=300, rung=rung) == ["eva"]


def test_take_pending_drains_so_one_doorbell_is_one_check():
    inbox_due.ring("eva@dimagi-ai.com")
    assert inbox_due.take_pending() == {"eva@dimagi-ai.com"}
    assert inbox_due.take_pending() == set()


def test_discovered_by_reports_the_trigger():
    assert inbox_due.discovered_by("eva", {"eva"}) == "push"
    assert inbox_due.discovered_by("hal", {"eva"}) == "poll"


# ── the discovery tag ────────────────────────────────────────────────────────


def test_the_turn_carries_how_it_was_discovered():
    client = FakeClient()
    r = _runner([{"id": "t1", "from": "x@y.com", "subject": "s", "messageCount": 1}])
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                      runner=r, discovered_by="push")
    assert client.enqueued[0]["origin_ref"]["discovered_by"] == "push"


def test_poll_is_the_default_tag():
    client = FakeClient()
    r = _runner([{"id": "t1", "from": "x@y.com", "subject": "s", "messageCount": 1}])
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c", runner=r)
    assert client.enqueued[0]["origin_ref"]["discovered_by"] == "poll"


# ── not paying twice for what we already know ────────────────────────────────


def test_a_known_thread_state_costs_no_subprocess_and_no_enqueue():
    """The wasted work this removes: a `gog gmail thread get` per unread thread
    per poll, forever, all concluding 'already tracked'."""
    client = FakeClient()
    threads = [{"id": "t1", "from": "x@y.com", "subject": "s", "messageCount": 1}]

    first_calls: list = []
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                      runner=_runner(threads, first_calls))
    assert len(client.enqueued) == 1
    assert any("thread" in c and "get" in c for c in first_calls)

    second_calls: list = []
    res = inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                            runner=_runner(threads, second_calls))
    assert res["seen"] == ["t1"]
    assert len(client.enqueued) == 1, "re-enqueued a thread it already knew"
    assert not any("thread" in c and "get" in c for c in second_calls), (
        "re-read a thread whose state has not changed"
    )


def test_a_new_reply_on_a_known_thread_is_checked_again():
    """Idempotency is keyed on (thread, messageCount) — a reply bumps the count,
    so the skip must not swallow it."""
    client = FakeClient()
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                      runner=_runner([{"id": "t1", "from": "x@y.com", "subject": "s",
                                       "messageCount": 1}]))
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                      runner=_runner([{"id": "t1", "from": "x@y.com", "subject": "s",
                                       "messageCount": 2}]))
    assert [e["idem"] for e in client.enqueued] == ["email-eva-t1-1", "email-eva-t1-2"]


def test_the_same_thread_id_in_two_mailboxes_is_two_states():
    client = FakeClient()
    thread = [{"id": "t1", "from": "x@y.com", "subject": "s", "messageCount": 1}]
    inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com", gog_client="c",
                      runner=_runner(thread))
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="c",
                      runner=_runner(thread))
    assert len(client.enqueued) == 2


def test_an_agents_own_reply_is_remembered_so_it_is_not_re_read():
    """The skip guard used to cost a subprocess on every poll for fourteen days."""
    client = FakeClient()
    threads = [{"id": "t1", "from": "eva@dimagi-ai.com", "subject": "s", "messageCount": 2}]

    def run_own(cmd, capture_output, text, timeout):
        if "thread" in cmd and "get" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"messages": [{"payload": {"headers": [
                    {"name": "From", "value": "eva@dimagi-ai.com"}]}}]}),
                stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"threads": threads}), stderr="")

    res = inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com",
                            gog_client="c", runner=run_own)
    assert res["skipped"] == ["t1"]

    calls: list = []

    def run_again(cmd, capture_output, text, timeout):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"threads": threads}), stderr="")

    res2 = inbox.check_inbox(client, "eva", mailbox="eva@dimagi-ai.com",
                             gog_client="c", runner=run_again)
    assert res2["seen"] == ["t1"]
    assert not any("thread" in c and "get" in c for c in calls)
    assert client.enqueued == []
