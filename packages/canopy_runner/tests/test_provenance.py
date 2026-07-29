"""What this runner reports about the code it is executing.

Spec: docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md
"""
from types import SimpleNamespace

import pytest

from canopy_runner import _build_info, provenance
from canopy_runner.client import Client


@pytest.fixture(autouse=True)
def _clean():
    provenance._reset_for_tests()
    yield
    provenance._reset_for_tests()


# --- code_sha: the quantity the staleness alert compares -------------------------


def test_prefers_the_stamped_build_over_git(monkeypatch):
    """An INSTALLED runner answers from _build_info and never shells out to git.

    Load-bearing beyond speed: a tool venv can sit inside some unrelated git
    repository, and asking git there would report a sha that has nothing to do
    with the runner's code."""
    monkeypatch.setattr(_build_info, "SHA", "a" * 40)
    monkeypatch.setattr(provenance.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not consult git when stamped"))
    assert provenance.code_sha() == "a" * 40


def test_falls_back_to_the_runner_sources_own_git_log(monkeypatch):
    """A SOURCE runner computes the same quantity live: the last commit touching
    the runner's own source dir — NOT the repo HEAD, which moves on every
    canopy-web commit and would alert on an unrelated frontend change."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="b" * 40 + "\n", stderr="", returncode=0)

    monkeypatch.setattr(_build_info, "SHA", "")
    monkeypatch.setattr(provenance.subprocess, "run", fake_run)
    assert provenance.code_sha() == "b" * 40

    src = str(provenance.runner_src_dir())
    assert captured["cmd"][:5] == ["git", "-C", src, "log", "-1"]
    # Path-scoped, or it degenerates into HEAD.
    assert captured["cmd"][-2:] == ["--", src]


def test_is_empty_when_git_fails(monkeypatch):
    """Fail-safe: unknown provenance must read as unknown, never as a guess. The
    supervisor stays silent on an empty sha rather than alerting on partial data."""
    monkeypatch.setattr(_build_info, "SHA", "")
    monkeypatch.setattr(provenance.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="", stderr="x", returncode=128))
    assert provenance.code_sha() == ""


def test_survives_git_being_absent(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(_build_info, "SHA", "")
    monkeypatch.setattr(provenance.subprocess, "run", boom)
    assert provenance.code_sha() == ""


def test_code_sha_is_pinned_for_the_process(monkeypatch):
    """It answers "which code did I IMPORT", which is fixed at process start. A
    `git pull` under a running daemon does not change the code in memory, so
    reporting the new sha before the restart would clear the staleness banner
    while the old code is still executing."""
    monkeypatch.setattr(_build_info, "SHA", "")
    calls = []

    def once(*a, **k):
        calls.append(1)
        return SimpleNamespace(stdout="c" * 40, stderr="", returncode=0)

    monkeypatch.setattr(provenance.subprocess, "run", once)
    for _ in range(5):
        provenance.code_sha()
    assert len(calls) == 1


# --- code_branch: re-read, unlike code_sha ---------------------------------------


def test_code_branch_is_re_read_after_its_ttl(monkeypatch):
    """Deliberately NOT pinned like code_sha: files the runner SPAWNS rather than
    imports (the CDP sidecar .mjs) change the instant the branch does, so this
    must reflect the checkout now."""
    branches = iter(["main", "feat-x"])
    monkeypatch.setattr(provenance.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            stdout=next(branches), stderr="", returncode=0))
    clock = {"t": 1000.0}
    assert provenance.code_branch(now_fn=lambda: clock["t"]) == "main"
    clock["t"] += 1  # inside the TTL — cached
    assert provenance.code_branch(now_fn=lambda: clock["t"]) == "main"
    clock["t"] += 60  # past it — re-read
    assert provenance.code_branch(now_fn=lambda: clock["t"]) == "feat-x"


# --- the client stamps every heartbeat -------------------------------------------


class _Recorder(Client):
    def __init__(self):
        super().__init__("http://x", "t")
        self.payloads = []

    def _call(self, method, path, body=None):
        self.payloads.append(body)
        return 200, {}


def test_every_heartbeat_carries_provenance_without_the_caller_passing_it(monkeypatch):
    """The bug this prevents: `services.heartbeat` assigns these unconditionally,
    so a heartbeat that omits one RESETS it server-side. Four of the loop's six
    call sites never passed a branch, so the wrong-branch banner blinked off
    whenever a lease-renewal or drain-one heartbeat landed between loop ticks."""
    # Drive the REAL functions rather than replacing them, so this also proves the
    # client calls the ones that actually ship.
    monkeypatch.setattr(_build_info, "SHA", "d" * 40)
    monkeypatch.setattr(provenance.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="main", stderr="", returncode=0))

    c = _Recorder()
    c.heartbeat("r-1", [])                       # the bare call every site can make
    c.heartbeat("r-1", [], degraded=True, note="cdp down")

    assert len(c.payloads) == 2
    for body in c.payloads:
        assert body["code_branch"] == "main"
        assert body["code_sha"] == "d" * 40
        assert body["code_version"] == provenance.version()


def test_an_explicit_value_still_wins(monkeypatch):
    """None means "stamp it"; a real value (including "") is honoured, so a caller
    that genuinely knows better — or a test — can still say so."""
    monkeypatch.setattr(_build_info, "SHA", "d" * 40)
    c = _Recorder()
    c.heartbeat("r-1", [], code_sha="", code_branch="odd", code_version="9.9.9")
    assert c.payloads[0]["code_sha"] == ""
    assert c.payloads[0]["code_branch"] == "odd"
    assert c.payloads[0]["code_version"] == "9.9.9"
