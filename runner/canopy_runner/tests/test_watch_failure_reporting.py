"""A runner that CANNOT arm a Gmail watch has to say so — and back off.

Two failures used to be invisible together. The arm retried every tick because a
failure never wrote the state file, and the server was told nothing because
`report_watch` was only called on the success path — so canopy-web kept showing
the last good expiry, and the mailbox only went loud days later as `watch.expired`,
which names the wrong cause. These pin both halves, plus the rule that a PAUSED
runner is silent (it is parked on purpose; that is not an error to page anyone about).
"""
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import gmail_watch, main

ADDRESS = "echo@dimagi-ai.com"
TOPIC = "projects/p/topics/t"
T0 = dt.datetime(2026, 8, 15, 12, 0, tzinfo=dt.UTC)


class FakeClient:
    def __init__(self):
        self.reports = []

    def runner_mailboxes(self):
        return [{"address": ADDRESS, "watch_topic": TOPIC}]

    def report_watch(self, address, expires_at, error=""):
        self.reports.append({"address": address, "expires_at": expires_at, "error": error})
        return {}


@pytest.fixture()
def cfg(tmp_path):
    return SimpleNamespace(
        mailboxes={"echo": {"account": ADDRESS, "client": "canopy"}},
        state_path=str(tmp_path / "runner-state.json"),
        gmail_watch_topic="",
    )


def _state(cfg) -> dict:
    path = Path(cfg.state_path).with_name("gmail-watch.json")
    return json.loads(path.read_text()) if path.exists() else {}


def _arm_fails(monkeypatch, exc=RuntimeError("401: unauthorized_client")):
    calls = []

    def boom(*a, **kw):
        calls.append(a)
        raise exc

    monkeypatch.setattr(gmail_watch, "arm", boom)
    return calls


def _arm_ok(monkeypatch, expires):
    calls = []

    def ok(*a, **kw):
        calls.append(a)
        return expires

    monkeypatch.setattr(gmail_watch, "arm", ok)
    return calls


# ── backoff ──────────────────────────────────────────────────────────────────


def test_a_failed_arm_is_not_retried_on_the_very_next_tick(cfg, monkeypatch):
    client = FakeClient()
    calls = _arm_fails(monkeypatch)

    main._maybe_rearm_watches(cfg, client, now=T0)
    assert len(calls) == 1

    # One second later: the old code retried here, and kept retrying every tick.
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(seconds=1))
    assert len(calls) == 1, "a failing arm must back off, not hammer every tick"


def test_the_retry_happens_once_the_backoff_elapses(cfg, monkeypatch):
    client = FakeClient()
    calls = _arm_fails(monkeypatch)

    main._maybe_rearm_watches(cfg, client, now=T0)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=2))
    assert len(calls) == 2

    # Second failure widens the window to 5 minutes, so 2 more minutes is too soon.
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=4))
    assert len(calls) == 2
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=8))
    assert len(calls) == 3


# ── telling the server ───────────────────────────────────────────────────────


def test_the_first_failure_is_not_reported(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    main._maybe_rearm_watches(cfg, client, now=T0)
    assert client.reports == [], "one blip is not an outage"


def test_a_repeated_failure_reports_that_the_mailbox_is_unwatched(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    main._maybe_rearm_watches(cfg, client, now=T0)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=2))

    assert len(client.reports) == 1
    report = client.reports[0]
    assert report["address"] == ADDRESS
    assert report["expires_at"] is None, "null expiry is how you say 'no watch'"
    assert "unauthorized_client" in report["error"]


def test_the_same_failure_is_reported_once_not_every_retry(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    for minutes in (0, 2, 8, 25, 60, 120):
        main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=minutes))
    assert len(client.reports) == 1, "the server coalesces, but do not spam it either"


def test_a_changed_error_is_reported_again(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    main._maybe_rearm_watches(cfg, client, now=T0)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=2))
    _arm_fails(monkeypatch, RuntimeError("no gog client credentials"))
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=8))

    assert len(client.reports) == 2
    assert "no gog client credentials" in client.reports[-1]["error"]


# ── recovery ─────────────────────────────────────────────────────────────────


def test_a_successful_arm_clears_the_failure_and_reports_the_expiry(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    main._maybe_rearm_watches(cfg, client, now=T0)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=2))

    expires = T0 + dt.timedelta(days=7)
    _arm_ok(monkeypatch, expires)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=8))

    assert client.reports[-1]["expires_at"] == expires
    assert client.reports[-1]["error"] == ""
    assert not _state(cfg).get("_failures", {}).get(ADDRESS)


# ── paused ───────────────────────────────────────────────────────────────────


def test_pausing_clears_an_outstanding_failure(cfg, monkeypatch):
    client = FakeClient()
    _arm_fails(monkeypatch)
    main._maybe_rearm_watches(cfg, client, now=T0)
    main._maybe_rearm_watches(cfg, client, now=T0 + dt.timedelta(minutes=2))
    assert len(client.reports) == 1

    main._clear_watch_failures(cfg, client)

    # Reported as resolved, and the local state forgets it, so resuming re-reports
    # from scratch rather than staying quiet about a still-broken mailbox.
    assert client.reports[-1]["expires_at"] is None
    assert client.reports[-1]["error"] == ""
    assert not _state(cfg).get("_failures", {}).get(ADDRESS)


def test_clearing_is_a_no_op_when_nothing_is_failing(cfg, monkeypatch):
    client = FakeClient()
    _arm_ok(monkeypatch, T0 + dt.timedelta(days=7))
    main._maybe_rearm_watches(cfg, client, now=T0)
    before = len(client.reports)

    main._clear_watch_failures(cfg, client)
    main._clear_watch_failures(cfg, client)
    assert len(client.reports) == before, "a parked healthy runner must stay silent"
