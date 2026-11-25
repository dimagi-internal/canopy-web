"""The laptop runner's vault keeper: `canopy vault zk keep` when this box's keeper
version moved, else on a slow reconcile; on its own clock with no version to go by."""
from __future__ import annotations

import subprocess

from canopy_runner import vault_keep
from canopy_runner.runner_sync import SyncBox


class _Done:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def test_a_pass_runs_the_keeper(monkeypatch):
    calls = []
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: "/usr/local/bin/canopy")
    out, ok = vault_keep.once(run=lambda argv, **kw: calls.append(argv) or _Done("agent ace\n  granted: user 9"))
    assert calls == [["/usr/local/bin/canopy", "vault", "zk", "keep"]]
    assert "granted: user 9" in out and ok


def test_no_cli_or_a_failure_is_quiet(monkeypatch):
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: None)
    assert vault_keep.once(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")), last="x") == ("x", False)
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: "/c")

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert vault_keep.once(run=boom, last="before") == ("before", False)
    assert vault_keep.once(run=lambda argv, **kw: _Done("x", returncode=1)) == ("x", False)


def test_when_a_pass_is_due():
    due = vault_keep.due
    assert due(0.0, at=None, kept=None, version=None)                          # the first pass
    # No version to go by (an older server): the clock.
    assert not due(100.0, at=0.0, kept=None, version=None)
    assert due(vault_keep.EVERY, at=0.0, kept=None, version=None)
    # A version: unmoved waits for the reconcile, moved runs after the short gap.
    assert not due(vault_keep.EVERY, at=0.0, kept=4, version=4)
    assert due(vault_keep.RECONCILE, at=0.0, kept=4, version=4)
    assert not due(1.0, at=0.0, kept=4, version=5)
    assert due(vault_keep.MIN_GAP, at=0.0, kept=4, version=5)
    # The last pass failed: the clock again.
    assert not due(vault_keep.MIN_GAP, at=0.0, kept=None, version=5)


def test_the_loop_runs_on_a_moved_version_and_can_be_turned_off(monkeypatch):
    seen, now, v = [], [0.0], [3]

    def fake_once(last="", **kw):
        seen.append((now[0], v[0]))
        return "same", True

    def sleep(s):
        now[0] += 5
        if now[0] == 50:
            v[0] = 4                                    # a ring
    monkeypatch.setattr(vault_keep, "once", fake_once)
    t = vault_keep.start(sleep=sleep, max_passes=2, version=lambda: v[0], clock=lambda: now[0])
    t.join(timeout=5)
    assert seen == [(0.0, 3), (50.0, 4)]
    monkeypatch.setenv("VAULT_KEEP", "0")
    assert vault_keep.start() is None


def test_the_sync_box_says_the_keeper_version_only_while_on_sync():
    class _Client:
        def __init__(self, body):
            self.body = body

        def get_sync(self, runner_id, etag):
            return 200, '"v1"', self.body
    box = SyncBox()
    assert box.keeper() is None
    assert box.refresh(_Client({"version": 1, "keeper_version": 9}), "r")
    assert box.keeper() == 9
    box2 = SyncBox()
    assert box2.refresh(_Client({"version": 1}), "r") and box2.keeper() is None
