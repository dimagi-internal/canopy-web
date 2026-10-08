import threading

import fakeredis
import pytest

from canopy_sdk.ondemand import Lease


def _r():
    return fakeredis.FakeRedis(decode_responses=True)


def test_lease_is_exclusive_and_namespaced():
    r = _r()
    a, b = Lease(r, "emod"), Lease(r, "mobile")
    assert a.acquire("x") and not a.acquire("y")
    assert b.acquire("y")
    assert a.holder() == "x" and r.get("ondemand:emod:lock") == "x"


def test_release_only_by_owner():
    l = Lease(_r(), "emod")
    l.acquire("x")
    assert not l.release("y") and l.release("x") and l.holder() == ""


def test_second_acquire_waits_not_fails():
    l = Lease(_r(), "emod")
    l.acquire("first")
    threading.Timer(0.3, lambda: l.release("first")).start()
    assert l.acquire_wait("second", timeout_s=3, poll_s=0.1)
    assert l.holder() == "second"


def test_acquire_wait_times_out():
    l = Lease(_r(), "emod")
    l.acquire("first")
    assert not l.acquire_wait("second", timeout_s=0.3, poll_s=0.05)
    assert l.holder() == "first"


def test_ttl_applied_and_refresh_only_by_owner():
    r = _r()
    l = Lease(r, "emod", ttl_s=100)
    l.acquire("x")
    r.expire("ondemand:emod:lock", 5)
    assert not l.refresh("y")
    assert r.ttl("ondemand:emod:lock") <= 5
    assert l.refresh("x")
    assert 5 < r.ttl("ondemand:emod:lock") <= 100


def test_key_override_keeps_existing_consumer_key():
    r = _r()
    l = Lease(r, "mobile", key="mobile:emulator:lock")
    l.acquire("x")
    assert r.get("mobile:emulator:lock") == "x"
    assert r.get("ondemand:mobile:lock") is None


class _NoEval:
    """Redis wrapper without EVAL, forcing the WATCH/MULTI fallbacks."""

    def __init__(self, r):
        self._r = r

    def eval(self, *a, **k):
        raise RuntimeError("EVAL unsupported")

    def __getattr__(self, name):
        return getattr(self._r, name)


def test_watch_multi_fallbacks_when_eval_unsupported():
    r = _r()
    l = Lease(_NoEval(r), "emod", ttl_s=100)
    l.acquire("x")
    assert not l.release("y") and not l.refresh("y")
    r.expire("ondemand:emod:lock", 5)
    assert l.refresh("x") and r.ttl("ondemand:emod:lock") > 5
    assert l.release("x") and l.holder() == ""
