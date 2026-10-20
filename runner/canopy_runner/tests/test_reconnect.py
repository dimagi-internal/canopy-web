"""A runner whose token canopy stopped accepting reconnects itself from the browser (#1705)."""
from pathlib import Path

from canopy_runner import reconnect
from canopy_runner.client import ClientError

DEAD = ClientError("POST /runners/r-1/heartbeat -> 401: b'{\"detail\": \"Authentication required\"}'")
BLIP = ClientError("POST /runners/r-1/heartbeat -> 502: b'bad gateway'")


def _watch(monkeypatch, *, key_active=False, rc=0, now=[1000.0]):
    monkeypatch.setattr(reconnect, "_key_signs_in", lambda rid: key_active)
    calls, restarts = [], []

    def pair(path, **kw):
        calls.append((path, kw))
        return rc

    w = reconnect.AuthWatch(Path("/x/runner.json"), "https://canopy", "r-1", pair_fn=pair,
                            restart=lambda: restarts.append(1), clock=lambda: now[0],
                            spawn=lambda fn: fn())
    return w, calls, restarts


def test_three_refused_heartbeats_open_a_reconnect_and_restart(monkeypatch):
    w, calls, restarts = _watch(monkeypatch)
    for _ in range(2):
        w.note(DEAD)
        assert not w.maybe_reconnect()
    w.note(DEAD)
    assert w.maybe_reconnect()
    assert calls[0][1]["reconnect"] is True and calls[0][1]["base_url"] == "https://canopy"
    assert restarts == [1]


def test_a_blip_or_a_good_tick_resets_the_count(monkeypatch):
    w, calls, _ = _watch(monkeypatch)
    w.note(DEAD)
    w.note(DEAD)
    w.note(BLIP)
    w.note(DEAD)
    w.ok()
    w.note(DEAD)
    assert not w.maybe_reconnect() and not calls


def test_an_active_device_key_means_nothing_to_reconnect(monkeypatch):
    w, calls, _ = _watch(monkeypatch, key_active=True)
    for _ in range(5):
        w.note(DEAD)
    assert not w.maybe_reconnect() and not calls


def test_a_failed_attempt_waits_before_the_next(monkeypatch):
    now = [1000.0]
    w, calls, restarts = _watch(monkeypatch, rc=1, now=now)
    for _ in range(3):
        w.note(DEAD)
    assert w.maybe_reconnect() and not restarts
    assert not w.maybe_reconnect()
    now[0] += reconnect.RETRY_SECONDS + 1
    assert w.maybe_reconnect() and len(calls) == 2
