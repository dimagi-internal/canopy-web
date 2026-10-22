"""The laptop runner's vault keeper: `canopy vault zk keep` on its own clock."""
from __future__ import annotations

import subprocess

from canopy_runner import vault_keep


class _Done:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


def test_a_pass_runs_the_keeper(monkeypatch):
    calls = []
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: "/usr/local/bin/canopy")
    out = vault_keep.once(run=lambda argv, **kw: calls.append(argv) or _Done("agent ace\n  granted: user 9"))
    assert calls == [["/usr/local/bin/canopy", "vault", "zk", "keep"]]
    assert "granted: user 9" in out


def test_no_cli_or_a_failure_is_quiet(monkeypatch):
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: None)
    assert vault_keep.once(run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")), last="x") == "x"
    monkeypatch.setattr(vault_keep, "canopy_cli", lambda: "/c")

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert vault_keep.once(run=boom, last="before") == "before"


def test_the_loop_runs_and_can_be_turned_off(monkeypatch):
    seen = []
    monkeypatch.setattr(vault_keep, "once", lambda last="", **kw: seen.append(last) or "same")
    t = vault_keep.start(every=0, sleep=lambda s: None, max_passes=3)
    t.join(timeout=5)
    assert seen == ["", "same", "same"]
    monkeypatch.setenv("VAULT_KEEP", "0")
    assert vault_keep.start() is None
