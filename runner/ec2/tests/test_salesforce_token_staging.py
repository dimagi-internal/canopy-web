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
    cr._restage_sf_token("t1")                       # real clock: far past expires_at -> restage
    assert len(cr.calls) == 2
    _path(tmp_path).write_text(json.dumps({"expires_at": 4_000_000_000}))
    cr._restage_sf_token("t1")                       # plenty of life left
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
