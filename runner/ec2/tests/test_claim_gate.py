"""The box says on its heartbeat why it is not claiming.

2026-10-24: an Eva chat turn sat queued 15 minutes behind cloud-ec2-2. The
server saw an online, ready, idle box; the box was skipping its own claim, and
only journald knew which of its limits said no. `claim_gate` is the one gate
both claim loops ask, and the `claim` health check reports it.
"""
from __future__ import annotations

import pytest

_MEMINFO = "MemTotal:        8388608 kB\nMemAvailable:    {avail} kB\n"


@pytest.fixture
def box(cloud_runner, monkeypatch):
    with cloud_runner._TURNS_LOCK:
        cloud_runner._IN_FLIGHT.clear()
    monkeypatch.setattr(cloud_runner, "MAX_CONCURRENT_TURNS", 4)
    monkeypatch.setattr(cloud_runner, "_read_meminfo", lambda: _MEMINFO.format(avail=3 * 1024 * 1024))
    monkeypatch.setattr(cloud_runner, "_NOT_CLAIMING_SINCE", None)
    return cloud_runner


def _in_flight(cr, *ids):
    with cr._TURNS_LOCK:
        for t in ids:
            cr._IN_FLIGHT[t] = None


def test_claiming_names_the_load_and_free_memory(box):
    _in_flight(box, "a" * 36)
    assert box.claim_gate() == (True, "", "claiming · 1/4 turns in flight · 3.0 GiB free")


def test_at_the_turn_cap_it_names_the_turns_and_how_long(box):
    _in_flight(box, "c1371487-aaaa", "0547f2f2-bbbb", "11111111-cccc", "22222222-dddd")
    assert box.claim_gate(now=1000.0)[:2] == (False, "turn_cap")
    claiming, reason, detail = box.claim_gate(now=1000.0 + 14 * 60)
    assert detail == ("not claiming for 14m: at turn cap 4/4 "
                      "(in flight: 0547f2f2, 11111111, 22222222, c1371487)")


def test_short_memory_names_free_against_the_floor(box, monkeypatch):
    monkeypatch.setattr(box, "_read_meminfo", lambda: _MEMINFO.format(avail=629_146))
    claiming, reason, detail = box.claim_gate(now=50.0)
    assert (claiming, reason) == (False, "memory")
    assert detail == "not claiming for <1m: 0.6 GiB free < 1.0 GiB floor (0/4 turns in flight)"


def test_a_refused_token_and_an_unknown_data_mode_are_named(box, monkeypatch):
    monkeypatch.setattr(box, "_TOKEN_REFUSED_AT", 1.0)
    assert box.claim_gate()[:2] == (False, "token_refused")
    monkeypatch.setattr(box, "_TOKEN_REFUSED_AT", 0.0)
    monkeypatch.setattr(box, "RUNNER_DATA_MODE", "bogus")
    claiming, reason, detail = box.claim_gate()
    assert (claiming, reason) == (False, "data_mode")
    assert "'bogus'" in detail


def test_the_streak_restarts_once_it_claims_again(box):
    _in_flight(box, "x1", "x2", "x3", "x4")
    box.claim_gate(now=0.0)
    assert "2h05m" in box.claim_gate(now=125 * 60)[2]
    with box._TURNS_LOCK:
        box._IN_FLIGHT.pop("x1")
    assert box.claim_gate(now=126 * 60)[0] is True
    _in_flight(box, "x1")
    assert "<1m" in box.claim_gate(now=127 * 60)[2]


def test_the_heartbeat_carries_the_claim_check(box):
    _in_flight(box, "x1", "x2", "x3", "x4")
    checks = {c["name"]: c for c in box.health_report()["checks"]}
    assert checks["claim"]["status"] == "warn"
    assert "at turn cap 4/4" in checks["claim"]["detail"]
    with box._TURNS_LOCK:
        box._IN_FLIGHT.clear()
    checks = {c["name"]: c for c in box.health_report()["checks"]}
    assert checks["claim"] == {"name": "claim", "status": "ok",
                               "detail": "claiming · 0/4 turns in flight · 3.0 GiB free"}


def test_both_claim_loops_ask_the_one_gate(box, monkeypatch):
    monkeypatch.setattr(box, "claim_gate", lambda **k: (False, "turn_cap", "x"))
    sent = []
    monkeypatch.setattr(box, "_ws_request", lambda *a, **k: sent.append(a) or {})
    assert box._claim_and_run_once(object(), "r1") is False
    assert sent == []
    import inspect
    src = inspect.getsource(box)
    assert src.count("if not claim_gate()[0]:") == 2
