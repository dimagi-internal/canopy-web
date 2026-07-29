"""Deterministic inbox trigger — gmail threads → email-origin turns."""
import json
from types import SimpleNamespace

import pytest

from canopy_runner import inbox


class FakeClient:
    def __init__(self, created=True):
        self.enqueued = []
        self._created = created

    def enqueue_turn(self, agent, origin, idem, *, prompt="", origin_ref=None, routing="prefer_local"):
        self.enqueued.append({"agent": agent, "origin": origin, "idem": idem,
                              "origin_ref": origin_ref, "prompt": prompt})
        return {"id": "t-x", "_created": self._created}


def _runner(threads):
    payload = json.dumps({"threads": threads})

    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")
    return run


def test_enqueues_one_turn_per_thread():
    client = FakeClient()
    r = _runner([
        {"id": "thr-1", "from": "Hal <hal@dimagi-ai.com>", "subject": "re: bednet", "messageCount": 3},
        {"id": "thr-2", "from": "x@y.com", "subject": "hi", "messageCount": 1},
    ])
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy", runner=r)
    assert res["new"] == ["thr-1", "thr-2"]
    assert client.enqueued[0]["origin"] == "email"
    assert client.enqueued[0]["origin_ref"]["thread_id"] == "thr-1"
    assert client.enqueued[0]["prompt"] == "/hal:turn --thread thr-1"


def test_idempotency_key_includes_message_count():
    client = FakeClient()
    r = _runner([{"id": "thr-1", "from": "Hal", "subject": "s", "messageCount": 3}])
    inbox.check_inbox(client, "hal", mailbox="m", gog_client="c", runner=r)
    assert client.enqueued[0]["idem"] == "email-hal-thr-1-3"  # a new reply (count 4) -> new key


def test_empty_inbox_enqueues_nothing():
    client = FakeClient()
    assert inbox.check_inbox(client, "hal", mailbox="m", gog_client="c", runner=_runner([])) == {"new": [], "seen": [], "skipped": []}
    assert client.enqueued == []


def test_gog_failure_raises_inboxerror():
    def fail(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="auth expired")
    with pytest.raises(inbox.InboxError, match="auth expired"):
        inbox.check_inbox(FakeClient(), "hal", mailbox="m", gog_client="c", runner=fail)


def test_skips_thread_when_newest_message_is_agents_own_reply():
    """The bug: a thread whose newest message is the agent's OWN reply must not fire a
    turn, even while it carries the UNREAD label. `from` in the search payload is the
    thread ORIGINATOR (a human here), so the guard must consult the newest sender."""
    client = FakeClient()
    r = _runner([{"id": "thr-1", "from": "Jonathan <jjackson@dimagi.com>",
                  "subject": "Feature Requests", "messageCount": 18}])
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=r, sender_of=lambda tid: "Hal <hal@dimagi-ai.com>")
    assert client.enqueued == []
    assert res["new"] == []
    assert res["skipped"] == ["thr-1"]


def test_enqueues_when_newest_message_is_from_human():
    """A genuine new inbound (newest message from someone other than the agent) fires."""
    client = FakeClient()
    r = _runner([{"id": "thr-2", "from": "x@y.com", "subject": "hi", "messageCount": 2}])
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=r, sender_of=lambda tid: "Someone <x@y.com>")
    assert res["new"] == ["thr-2"]
    assert len(client.enqueued) == 1


def test_enqueues_when_newest_sender_unknown_fail_open():
    """Fail open: if the newest sender can't be determined, enqueue — a rare spurious
    turn is cheaper than a missed reply to a real inbound."""
    client = FakeClient()
    r = _runner([{"id": "thr-3", "from": "x@y.com", "subject": "hi", "messageCount": 1}])
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=r, sender_of=lambda tid: None)
    assert res["new"] == ["thr-3"]


def test_newest_sender_reads_last_messages_from_header():
    payload = json.dumps({"messages": [
        {"payload": {"headers": [{"name": "From", "value": "Sarvesh <stewari@dimagi.com>"}]}},
        {"payload": {"headers": [{"name": "From", "value": "Hal <hal@dimagi-ai.com>"}]}},
    ]})

    def run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")
    assert inbox.newest_sender("hal@dimagi-ai.com", "canopy", "thr-1", runner=run) == "hal <hal@dimagi-ai.com>"


def test_newest_sender_returns_none_on_gog_failure():
    def fail(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")
    assert inbox.newest_sender("m", "c", "thr-1", runner=fail) is None
