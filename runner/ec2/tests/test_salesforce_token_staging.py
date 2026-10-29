"""An agent turn's Salesforce access token is staged per TURN by the canopy CLI.

Pinned: the CLI is called with the runner bearer + runner/turn ids; exit 3 (no connection)
is silent and clears a stale file; other failures warn (never with a token) and the turn
runs; the heartbeat re-stages only near expiry; the file goes when the turn ends.
"""
from __future__ import annotations

import json
import subprocess

import pytest


@pytest.fixture()
def cr(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cloud_runner, "_bearer", lambda min_life=0: "runner-bearer")
    monkeypatch.setattr(cloud_runner, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(cloud_runner, "_SF_STAGED", {})
    logs = []
    monkeypatch.setattr(cloud_runner, "_log", lambda m, *a, **k: logs.append(m))
    cloud_runner.calls = []
    cloud_runner.logs = logs
    return cloud_runner


def _path(tmp_path, slug="eva"):
    return tmp_path / ".canopy" / "connections" / slug / "salesforce.json"


def _cli(cr, monkeypatch, tmp_path, *, code=0, expires=None, stderr=""):
    def run(argv, **kw):
        cr.calls.append((argv, kw))
        if code == 0:
            p = _path(tmp_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"access_token": "SECRET", "expires_at": expires}))
        return subprocess.CompletedProcess(argv, code, "", stderr)
    monkeypatch.setattr(cr.subprocess, "run", run)


def test_stages_through_the_cli_with_the_runner_bearer(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_sf_token("r1", "t1", "eva", now=0)
    argv, kw = cr.calls[0]
    assert argv == ["/bin/canopy", "cred", "salesforce-token", "--runner", "--agent", "eva",
                    "--runner-id", "r1", "--turn", "t1"]
    assert kw["env"]["CANOPY_TOKEN"] == "runner-bearer" and kw["timeout"] == 120
    assert _path(tmp_path).exists() and "SECRET" not in " ".join(cr.logs)


def test_exit_3_is_silent_and_removes_a_stale_file(cr, monkeypatch, tmp_path):
    _path(tmp_path).parent.mkdir(parents=True)
    _path(tmp_path).write_text(json.dumps({"expires_at": 1}))
    _cli(cr, monkeypatch, tmp_path, code=3, stderr="not_connected")
    cr._stage_sf_token("r1", "t1", "eva", now=100)
    assert not _path(tmp_path).exists() and cr.logs == []


def test_other_failure_warns_and_the_turn_runs(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, code=1, stderr="boom")
    cr._stage_sf_token("r1", "t1", "eva", now=0)
    assert any("warn" in m and "boom" in m for m in cr.logs)


def test_heartbeat_restages_only_near_expiry(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_sf_token("r1", "t1", "eva", now=0)
    cr._restage_sf_token("t1", now=9_500)            # 500s of life left: restage
    assert len(cr.calls) == 2
    cr._restage_sf_token("t1", now=5_000)            # 5000s left: leave it
    assert len(cr.calls) == 2


def test_file_removed_at_turn_end(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_sf_token("r1", "t1", "eva", now=0)
    cr._unstage_sf_token("t1")
    assert not _path(tmp_path).exists()


def test_no_agent_stages_nothing(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path)
    cr._stage_sf_token("r1", "t1", "")
    assert cr.calls == []


def test_sensitive_paths_hide_the_connections_dir():
    from canopy_runner import native_permissions as np
    assert "~/.canopy/connections/**" in np.SENSITIVE_PATHS


def test_two_turns_of_one_agent_share_the_file(cr, monkeypatch, tmp_path):
    _cli(cr, monkeypatch, tmp_path, expires=10_000)
    cr._stage_sf_token("r1", "A", "eva", now=0)
    cr._stage_sf_token("r1", "B", "eva", now=0)
    cr._unstage_sf_token("A")
    assert _path(tmp_path).exists()          # B still holds it
    cr._unstage_sf_token("B")
    assert not _path(tmp_path).exists()


def test_a_turn_holds_before_the_cli_runs(cr, monkeypatch, tmp_path):
    seen = {}

    def run(argv, **kw):
        seen["held"] = dict(cr._SF_STAGED)
        return subprocess.CompletedProcess(argv, 1, "", "x")
    monkeypatch.setattr(cr.subprocess, "run", run)
    cr._stage_sf_token("r1", "t1", "eva", now=0)
    assert "t1" in seen["held"] and cr._SF_STAGED == {}     # dropped after the failure


def test_startup_clears_leftovers(cr, tmp_path):
    f = _path(tmp_path)
    f.parent.mkdir(parents=True)
    f.write_text("{}")
    cr._clear_stale_sf_tokens()
    assert not f.exists()


# ---- the wiring: which turns get a token --------------------------------------------

from test_confined_turns import _turn, cr as turn_cr  # noqa: E402,F401


@pytest.fixture()
def wired(turn_cr, monkeypatch, tmp_path):
    mod, calls = turn_cr
    monkeypatch.setattr(mod.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mod, "_bearer", lambda min_life=0: "b")
    monkeypatch.setattr(mod, "_canopy_cli", lambda: "/bin/canopy")
    monkeypatch.setattr(mod, "_SF_STAGED", {})
    sf = []
    seen = {}
    real_exec = mod.execute_prompt

    def run(argv, **kw):
        if "salesforce-token" in argv:
            sf.append(argv)
            p = _path(tmp_path, "ace")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"expires_at": 9e12}))
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(mod.subprocess, "run", run)

    def execute(*a, **k):
        seen["file_during_turn"] = _path(tmp_path, "ace").exists()
        return real_exec(*a, **k)
    monkeypatch.setattr(mod, "execute_prompt", execute)
    return mod, sf, seen, tmp_path


def test_a_confined_turn_is_never_staged(wired):
    mod, sf, seen, _ = wired
    mod._run_turn("r-1", _turn())
    assert sf == [] and seen["file_during_turn"] is False


def test_an_ordinary_agent_turn_is_staged_and_cleaned_up(wired):
    mod, sf, seen, tmp_path = wired
    mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert len(sf) == 1 and sf[0][-4:] == ["--runner-id", "r-1", "--turn", sf[0][-1]]
    assert seen["file_during_turn"] is True
    assert not _path(tmp_path, "ace").exists()


def test_an_abrupt_exit_after_staging_still_unstages(wired, monkeypatch):
    mod, sf, _, tmp_path = wired

    def boom(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(mod, "execute_prompt", boom)
    with pytest.raises(KeyboardInterrupt):
        mod._run_turn("r-1", _turn(caller_context={"profile": "full", "capability": None}))
    assert len(sf) == 1 and not _path(tmp_path, "ace").exists() and mod._SF_STAGED == {}
