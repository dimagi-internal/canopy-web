"""The cloud runner's keeper pass: `canopy vault zk keep` in the background, never
blocking or failing the loop. With a `/sync` answer that carries `keeper_version` it
runs when that moved, and otherwise only on KEEP_RECONCILE_SECONDS; without one, on
its own clock, at most every KEEP_INTERVAL_SECONDS."""
from __future__ import annotations

import subprocess


class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _run_inline(cloud_runner, monkeypatch):
    """Run the keeper's thread body in place, so a test sees its effect."""
    class Inline:
        def __init__(self, target, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

        def is_alive(self):
            return False
    monkeypatch.setattr(cloud_runner.threading, "Thread", Inline)


def test_a_pass_runs_the_keeper_and_logs_a_grant(cloud_runner, monkeypatch):
    calls, logs = [], []
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Done("agent ace\n  granted: user 9"))
    monkeypatch.setattr(cloud_runner, "_log", logs.append)
    _run_inline(cloud_runner, monkeypatch)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True
    assert calls[0][1:] == ["vault", "zk", "keep"]
    assert logs and "granted: user 9" in logs[0]


def test_the_keeper_is_told_this_boxs_canopy_web(cloud_runner, monkeypatch):
    """The keeper resolves canopy-web from CANOPY_WEB_API_URL alone, else production."""
    seen = []
    monkeypatch.setattr(cloud_runner, "BASE_URL", "https://canopy.staging.example")
    monkeypatch.delenv("CANOPY_WEB_API_URL", raising=False)
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda argv, **kw: seen.append(kw["env"]) or _Done(""))
    monkeypatch.setattr(cloud_runner, "_log", lambda *a, **k: None)
    _run_inline(cloud_runner, monkeypatch)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True
    assert seen[0]["CANOPY_WEB_API_URL"] == "https://canopy.staging.example"


def test_passes_are_spaced_and_repeats_are_quiet(cloud_runner, monkeypatch):
    calls, logs = [], []
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Done("agent ace\n  waiting: machine"))
    monkeypatch.setattr(cloud_runner, "_log", logs.append)
    _run_inline(cloud_runner, monkeypatch)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True
    assert cloud_runner.maybe_keep_vaults(now=1010.0) is False          # not due yet
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_INTERVAL_SECONDS) is True
    assert len(calls) == 2 and len(logs) == 1                           # same "waiting" said once


def test_a_failing_keeper_never_raises_and_can_be_turned_off(cloud_runner, monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    logs = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", boom)
    monkeypatch.setattr(cloud_runner, "_log", logs.append)
    _run_inline(cloud_runner, monkeypatch)
    assert cloud_runner.maybe_keep_vaults(now=5000.0) is True
    assert "could not run" in logs[0]
    monkeypatch.setenv("VAULT_KEEP", "0")
    assert cloud_runner.maybe_keep_vaults(now=99999.0) is False


def _synced(cloud_runner, keeper_version):
    cloud_runner._SYNC.update(supported=True, answer={"version": 1, "keeper_version": keeper_version})


def test_with_a_keeper_version_an_unmoved_one_waits_for_the_reconcile(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done(""))
    monkeypatch.setattr(cloud_runner, "_log", lambda *a, **k: None)
    _run_inline(cloud_runner, monkeypatch)
    _synced(cloud_runner, 7)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True                     # the first pass
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_INTERVAL_SECONDS) is False
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_RECONCILE_SECONDS) is True
    assert len(calls) == 2


def test_a_moved_keeper_version_runs_a_pass_at_once(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_runner.subprocess, "run", lambda argv, **kw: calls.append(argv) or _Done(""))
    monkeypatch.setattr(cloud_runner, "_log", lambda *a, **k: None)
    _run_inline(cloud_runner, monkeypatch)
    _synced(cloud_runner, 7)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True
    _synced(cloud_runner, 8)
    assert cloud_runner.maybe_keep_vaults(now=1005.0) is False                    # passes stay spaced
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_MIN_GAP_SECONDS) is True
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + 2 * cloud_runner.KEEP_MIN_GAP_SECONDS) is False
    assert len(calls) == 2


def test_a_failed_pass_is_tried_again_on_the_clock(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Done("", "boom", returncode=1))
    monkeypatch.setattr(cloud_runner, "_log", lambda *a, **k: None)
    _run_inline(cloud_runner, monkeypatch)
    _synced(cloud_runner, 7)
    assert cloud_runner.maybe_keep_vaults(now=1000.0) is True
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_MIN_GAP_SECONDS) is False
    assert cloud_runner.maybe_keep_vaults(now=1000.0 + cloud_runner.KEEP_INTERVAL_SECONDS) is True
    assert len(calls) == 2
