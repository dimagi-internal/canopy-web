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
    assert inbox.check_inbox(client, "hal", mailbox="m", gog_client="c", runner=_runner([])) == {"new": [], "seen": [], "skipped": [], "coalesced": []}
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


# --- CloudWatch alarm pairs: one incident, one turn -------------------------------
#
# Real subjects and sender from the 2026-09-05 `labs-jj-web-cpu-high` incident, which
# spawned two hal sessions for one alarm transition.

SNS = "Labs Alerts <no-reply@sns.amazonaws.com>"
_ALARM = 'ALARM: "labs-jj-web-cpu-high" in US East (N. Virginia)'
_OK = 'OK: "labs-jj-web-cpu-high" in US East (N. Virginia)'


def _alarm_pair():
    return [
        {"id": "thr-alarm", "from": SNS, "subject": _ALARM, "messageCount": 1},
        {"id": "thr-ok", "from": SNS, "subject": _OK, "messageCount": 1},
    ]


@pytest.fixture(autouse=True)
def _clear_seen_state():
    """`_seen_state` is module-global and these tests reuse thread ids."""
    inbox._seen_state.clear()
    inbox._alarm_enqueued.clear()
    yield
    inbox._seen_state.clear()
    inbox._alarm_enqueued.clear()


def _clock(t):
    """A frozen wall clock; `t` is seconds, origin arbitrary."""
    return lambda: float(t)


def test_alarm_key_parses_both_states():
    assert inbox.alarm_key({"from": SNS, "subject": _ALARM}) == ("ALARM", "labs-jj-web-cpu-high")
    assert inbox.alarm_key({"from": SNS, "subject": _OK}) == ("OK", "labs-jj-web-cpu-high")


def test_alarm_key_ignores_non_sns_sender():
    """A human writing `OK: "the deploy"` is not an alarm and must never be coalesced."""
    assert inbox.alarm_key({"from": "Jonathan <jjackson@dimagi.com>",
                            "subject": 'OK: "the deploy" in staging'}) is None


def test_alarm_key_ignores_unparseable_subject():
    assert inbox.alarm_key({"from": SNS, "subject": "your monthly AWS bill"}) is None
    assert inbox.alarm_key({"from": SNS, "subject": "OK: no quotes here"}) is None


def test_ok_thread_is_coalesced_into_its_alarm_turn():
    """The bug: ALARM: and OK: are two threads for ONE incident, so a single transition
    enqueued two turns — and the OK: half can never produce a finding."""
    client = FakeClient()
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(_alarm_pair()), sender_of=lambda tid: SNS.lower())
    assert res["new"] == ["thr-alarm"]
    assert res["coalesced"] == ["thr-ok"]
    assert [e["prompt"] for e in client.enqueued] == ["/hal:turn --thread thr-alarm"]


def test_coalescing_holds_regardless_of_thread_order():
    """The OK: arrives BEFORE its ALARM: in the batch — the pairing is by alarm name,
    not by position, so ordering must not change the outcome."""
    client = FakeClient()
    threads = list(reversed(_alarm_pair()))
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(threads), sender_of=lambda tid: SNS.lower())
    assert res["new"] == ["thr-alarm"]
    assert res["coalesced"] == ["thr-ok"]


def test_lone_ok_with_no_matching_alarm_still_fires():
    """Fail-open: an OK: whose ALARM: is not in the batch is the only signal there is,
    so it must still become a turn. Never silently drop a message."""
    client = FakeClient()
    threads = [{"id": "thr-ok", "from": SNS, "subject": _OK, "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(threads), sender_of=lambda tid: SNS.lower())
    assert res["new"] == ["thr-ok"]
    assert res["coalesced"] == []


def test_ok_for_a_different_alarm_is_not_coalesced():
    """Pairing is per alarm NAME — an unrelated alarm's OK: must not be swallowed by a
    live ALARM: for something else."""
    client = FakeClient()
    threads = _alarm_pair() + [
        {"id": "thr-other-ok", "from": SNS, "messageCount": 1,
         "subject": 'OK: "labs-jj-web-worker-crash-loop" in US East (N. Virginia)'},
    ]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(threads), sender_of=lambda tid: SNS.lower())
    assert res["new"] == ["thr-alarm", "thr-other-ok"]
    assert res["coalesced"] == ["thr-ok"]


def test_alarm_thread_itself_is_never_coalesced():
    """Only the OK: side is ever suppressed; the ALARM: always owns the incident."""
    client = FakeClient()
    threads = [{"id": "thr-alarm", "from": SNS, "subject": _ALARM, "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(threads), sender_of=lambda tid: SNS.lower())
    assert res["new"] == ["thr-alarm"]
    assert res["coalesced"] == []


def test_coalescing_skips_the_thread_get_subprocess():
    """The coalesce check sits ABOVE `sender_of` (a `gog gmail thread get` subprocess),
    so a suppressed OK: must not cost one."""
    looked_up = []

    def sender_of(tid):
        looked_up.append(tid)
        return SNS.lower()

    inbox.check_inbox(FakeClient(), "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_pair()), sender_of=sender_of)
    assert "thr-ok" not in looked_up


def test_non_alarm_mail_is_completely_unaffected():
    """The regression guard: ordinary human mail must route exactly as before."""
    client = FakeClient()
    threads = [
        {"id": "thr-1", "from": "Jonathan <jjackson@dimagi.com>", "subject": "re: bednet",
         "messageCount": 3},
        {"id": "thr-2", "from": "x@y.com", "subject": "hi", "messageCount": 1},
    ]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(threads), sender_of=lambda tid: "x@y.com")
    assert res["new"] == ["thr-1", "thr-2"]
    assert res["coalesced"] == []


# --- one incident, one turn: the REPEAT and the LATE recovery ---------------------
#
# From hal, 2026-09-07. `labs-jj-web-cpu-high-actionable` transitioned to ALARM at
# 12:23:56, 12:29:21 and 12:34:21 UTC for ONE incident, and its `OK:` landed at 12:48
# — 24 minutes after the `ALARM:`, by which time that thread had been read and closed
# out. Result: three turns on the ALARM thread plus a fourth on the OK thread.

def _alarm_thread(count):
    """The SAME Gmail thread, re-fired: CloudWatch threads by subject, so a repeat is a
    bumped messageCount, which was a fresh idempotency key."""
    return [{"id": "thr-alarm", "from": SNS, "subject": _ALARM, "messageCount": count}]


def test_refired_alarm_on_the_same_thread_does_not_enqueue_again():
    client = FakeClient()
    first = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                              runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                              clock=_clock(0))
    assert first["new"] == ["thr-alarm"]

    # +5m21s and +10m25s, the real spacing of the two re-fires.
    for t, count in ((321, 2), (625, 3)):
        res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                                runner=_runner(_alarm_thread(count)),
                                sender_of=lambda tid: SNS.lower(), clock=_clock(t))
        assert res["new"] == [], f"re-fire at +{t}s enqueued a second turn"
        assert res["coalesced"] == ["thr-alarm"]

    assert len(client.enqueued) == 1, "one incident must produce exactly one turn"


def test_alarm_refiring_after_the_window_is_a_new_incident():
    """The suppression is bounded on purpose — a sustained condition must re-notify."""
    client = FakeClient()
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(0))
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(_alarm_thread(2)), sender_of=lambda tid: SNS.lower(),
                            clock=_clock(inbox.ALARM_REPEAT_WINDOW_S + 1))
    assert res["new"] == ["thr-alarm"]
    assert len(client.enqueued) == 2


def test_a_coalesced_refire_does_not_extend_its_own_suppression():
    """Otherwise an alarm flapping for an hour is reported once and then held down for
    the whole hour by its own repeats — silence that looks like health."""
    client = FakeClient()
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(0))
    # A re-fire near the end of the window, coalesced...
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(2)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(inbox.ALARM_REPEAT_WINDOW_S - 60))
    # ...must not push the window out: just past the ORIGINAL window, we notify again.
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(_alarm_thread(3)), sender_of=lambda tid: SNS.lower(),
                            clock=_clock(inbox.ALARM_REPEAT_WINDOW_S + 1))
    assert res["new"] == ["thr-alarm"]


def test_ok_is_coalesced_even_when_its_alarm_was_an_earlier_batch():
    """#670 paired them only within one poll, so a recovery arriving after its `ALARM:`
    had been read fell straight through and spawned a turn with nothing to do."""
    client = FakeClient()
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(0))
    ok_only = [{"id": "thr-ok", "from": SNS, "subject": _OK, "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(ok_only), sender_of=lambda tid: SNS.lower(),
                            clock=_clock(24 * 60))          # the real 24-minute gap
    assert res["new"] == []
    assert res["coalesced"] == ["thr-ok"]
    assert len(client.enqueued) == 1


def test_ok_with_no_recent_alarm_still_enqueues():
    """The fail-open direction: an `OK:` we cannot pair with anything is not suppressed.
    Covers the alarm-creation `N/A -> OK` notification, which has no `ALARM:` at all."""
    client = FakeClient()
    ok_only = [{"id": "thr-ok", "from": SNS, "subject": _OK, "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(ok_only), sender_of=lambda tid: SNS.lower(),
                            clock=_clock(0))
    assert res["new"] == ["thr-ok"]


def test_a_different_alarm_is_never_suppressed_by_an_unrelated_incident():
    client = FakeClient()
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(0))
    other = [{"id": "thr-other", "from": SNS,
              "subject": 'ALARM: "labs-jj-rds-connections-high" in US East (N. Virginia)',
              "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(other), sender_of=lambda tid: SNS.lower(),
                            clock=_clock(60))
    assert res["new"] == ["thr-other"]


def test_a_human_subject_that_looks_like_a_refire_is_never_suppressed():
    """`alarm_key` gates all of this on the SNS sender; a person is never coalesced."""
    client = FakeClient()
    inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                      runner=_runner(_alarm_thread(1)), sender_of=lambda tid: SNS.lower(),
                      clock=_clock(0))
    human = [{"id": "thr-human", "from": "Jonathan <jjackson@dimagi.com>",
              "subject": _ALARM, "messageCount": 1}]
    res = inbox.check_inbox(client, "hal", mailbox="hal@dimagi-ai.com", gog_client="canopy",
                            runner=_runner(human), sender_of=lambda tid: "jjackson@dimagi.com",
                            clock=_clock(60))
    assert res["new"] == ["thr-human"]
