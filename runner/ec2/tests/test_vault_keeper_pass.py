"""The cloud runner's keeper pass: `canopy vault zk keep` on its own clock, in the
background, at most every KEEP_INTERVAL_SECONDS, never blocking or failing the loop."""
from __future__ import annotations

import subprocess


class _Done:
    def __init__(self, stdout="", stderr=""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, 0


def _run_inline(cloud_runner, monkeypatch):
    """Run the keeper's thread body in place, so a test sees its effect."""
    class Inline:
        def __init__(self, target, **kw):
            self._target = target

        def start(self):
            self._target()

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
