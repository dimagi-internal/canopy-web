"""Refresh on a laptop: the runner page's Refresh updates plugins and the canopy CLI
in place, and the heartbeat reports when it finished (canopy-web#1763)."""
import json
import subprocess
from pathlib import Path

import pytest
from canopy_runner import cli_refresh, desktop, refresh

PLUGINS = [
    {"id": "canopy@canopy", "scope": "user"},
    {"id": "ada@ada", "scope": "user"},
    {"id": "ada@ada", "scope": "local"},       # a worktree's copy: not this account's
    {"id": "hal@hal", "scope": "local"},
]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(refresh, "_running", False)
    monkeypatch.setattr(refresh, "_last_started", 0.0)
    monkeypatch.setattr(refresh, "_report", None)
    monkeypatch.setattr(desktop, "claude_cli", lambda: Path("/bin/claude"))
    monkeypatch.setattr(cli_refresh, "check", lambda **kw: "current")


def _runner(calls, fail=()):
    def run(argv, **kw):
        calls.append(argv[1:])
        if argv[1:3] == ["plugin", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(PLUGINS), "")
        code = 1 if argv[1:3] == ["plugin", "update"] and argv[3] in fail else 0
        return subprocess.CompletedProcess(argv, code, "", "nope" if code else "")
    return run


def test_updates_marketplaces_and_this_accounts_plugins():
    calls = []
    check = refresh.prepare(run=_runner(calls))
    assert ["plugin", "marketplace", "update"] in calls
    updates = [c[2] for c in calls if c[:2] == ["plugin", "update"]]
    assert updates == ["ada@ada", "canopy@canopy"]
    assert check == {"name": "refresh", "status": "ok",
                     "detail": "2 of 2 plugins updated; canopy CLI current"}


def test_a_plugin_that_will_not_update_shows_on_the_runner_page():
    check = refresh.prepare(run=_runner([], fail={"ada@ada"}))
    assert check["status"] == "warn"
    assert "did not update: ada@ada" in check["detail"]


def test_no_claude_cli_still_refreshes_the_canopy_cli(monkeypatch):
    monkeypatch.setattr(desktop, "claude_cli", lambda: None)
    check = refresh.prepare(run=_runner([]))
    assert check["status"] == "warn" and check["detail"].endswith("canopy CLI current")


def test_pending_starts_one_refresh_and_reports_it_on_the_heartbeat():
    assert refresh.health() is None
    t = refresh.observe({"refresh_pending": True}, run=_runner([]), now=10_000.0)
    t.join(5)
    report = refresh.health()
    assert report["checks"][0]["name"] == "refresh"
    # bootstrapped_at is what discharges Runner.refresh_pending on the server.
    assert report["bootstrapped_at"] == report["checked_at"] > 0


def test_not_pending_does_nothing():
    assert refresh.observe({"refresh_pending": False}, run=_runner([])) is None
    assert refresh.observe(None) is None


def test_never_more_than_once_per_interval():
    calls = []
    refresh.observe({"refresh_pending": True}, run=_runner(calls), now=10_000.0).join(5)
    n = len(calls)
    assert refresh.observe({"refresh_pending": True}, run=_runner(calls), now=10_060.0) is None
    assert len(calls) == n
    later = 10_000.0 + refresh.MIN_INTERVAL_SECONDS
    refresh.observe({"refresh_pending": True}, run=_runner(calls), now=later).join(5)
    assert len(calls) > n


def test_a_refresh_that_raises_never_takes_the_runner_down():
    def boom(argv, **kw):
        raise RuntimeError("disk full")
    refresh.observe({"refresh_pending": True}, run=boom, now=10_000.0).join(5)
    assert refresh.health() is None
    assert refresh._running is False
