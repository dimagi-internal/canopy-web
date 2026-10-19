"""The credential probe: "ready" means a credential just answered a prompt.

A box used to report ready the moment a value was STORED, so a capped or
signed-out token read as healthy until a real turn failed on it (cloud-ec2-fork,
2026-10-18). Each credential is now asked one short question with no tools, and
the answer is what the runner page and readiness show.
"""
from __future__ import annotations

import json
import os
import subprocess
import types

import pytest

CAP = "You've hit your weekly limit · resets 11pm (UTC)"


@pytest.fixture()
def cr(cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-ec2-fork")
    monkeypatch.setattr(cloud_runner, "PROBE_ENABLED", True)
    cloud_runner._CLAUDE_CREDS[:] = [
        ("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1"),
        ("api-key", "ANTHROPIC_API_KEY", "sk"),
    ]
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cloud_runner._PROBES.clear()
    return cloud_runner


def _fake_run(cr, monkeypatch, *, stdout="", stderr="", returncode=0, raises=None):
    seen = {}

    def run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        if raises is not None:
            raise raises
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(cr.subprocess, "run", run)
    return seen


# ── the environment a probe runs in ─────────────────────────────────────────

def test_env_for_a_token_sets_only_that_var(cr):
    before = dict(os.environ)
    env = cr._credential_env(1)
    assert env["ANTHROPIC_API_KEY"] == "sk"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env0 = cr._credential_env(0)
    assert env0["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"
    assert "ANTHROPIC_API_KEY" not in env0
    # Turns run concurrently: probing must never move the process's credential.
    assert dict(os.environ) == before


def test_env_for_a_login_points_config_dir_at_it(cr, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "ambient")
    login_dir = str(tmp_path / "logins" / "a44d94c3")
    cr._CLAUDE_CREDS[:] = [("box-login:a44d94c3", "CLAUDE_CONFIG_DIR", login_dir)]
    env = cr._credential_env(0)
    assert env["CLAUDE_CONFIG_DIR"] == login_dir
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient"


# ── classification ──────────────────────────────────────────────────────────

def test_probe_classifies_the_weekly_cap(cr, monkeypatch):
    seen = _fake_run(cr, monkeypatch, returncode=1,
                     stdout=json.dumps({"is_error": True, "result": CAP}))
    res = cr._probe_credential(0)
    assert res["label"] == "subscription-1"
    assert res["status"] == "capped"
    assert res["reset"] is not None
    assert "weekly limit" in res["detail"]
    # One short turn, no tools, never in a turn's worktree.
    assert seen["argv"][1:] == ["-p", "Reply with OK", "--output-format", "json", "--max-turns", "1"]
    assert seen["kw"]["timeout"] == cr.PROBE_TIMEOUT_SECONDS == 90
    assert seen["kw"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-1"


def test_probe_classifies_not_logged_in(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=1, stdout="Not logged in · Please run /login\n")
    res = cr._probe_credential(0)
    assert res["status"] == "dead"
    assert res["reset"] is None


def test_probe_ok(cr, monkeypatch):
    _fake_run(cr, monkeypatch, stdout=json.dumps({"is_error": False, "result": "OK"}))
    assert cr._probe_credential(1)["status"] == "ok"


def test_probe_timeout_is_unknown(cr, monkeypatch):
    _fake_run(cr, monkeypatch, raises=subprocess.TimeoutExpired(["claude"], 90))
    res = cr._probe_credential(0)
    assert res["status"] == "unknown"
    assert "90" in res["detail"]


def test_probe_crash_is_unknown(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=2, stdout="Segmentation fault")
    assert cr._probe_credential(0)["status"] == "unknown"


def test_probe_off_is_unknown_and_runs_nothing(cr, monkeypatch):
    monkeypatch.setattr(cr, "PROBE_ENABLED", False)
    seen = _fake_run(cr, monkeypatch, stdout="{}")
    assert cr._probe_credential(0)["status"] == "unknown"
    assert "argv" not in seen


# ── what a probe feeds ──────────────────────────────────────────────────────

def test_record_probe_sets_the_health_check(cr, monkeypatch):
    _fake_run(cr, monkeypatch, returncode=1,
              stdout=json.dumps({"is_error": True, "result": CAP}))
    cr._record_probe(cr._probe_credential(0))
    check = cr._HEALTH["claude.probe.subscription-1"]
    assert check["status"] == "warn"
    assert "resets" in check["detail"]
    assert cr._PROBES["subscription-1"]["status"] == "capped"


@pytest.mark.parametrize("status,check", [
    ("ok", "ok"), ("dead", "fail"), ("unknown", "warn"),
])
def test_record_probe_maps_each_state(cr, status, check):
    cr._record_probe({"label": "api-key", "status": status, "detail": "why",
                      "reset": None, "at": 0.0})
    assert cr._HEALTH["claude.probe.api-key"]["status"] == check
    if status == "dead":
        assert "not signed in" in cr._HEALTH["claude.probe.api-key"]["detail"]


def test_a_removed_credential_drops_its_probe_check(cr):
    cr._record_probe({"label": "api-key", "status": "ok", "detail": "", "reset": None, "at": 0.0})
    cr._CLAUDE_CREDS[:] = [("subscription-1", "CLAUDE_CODE_OAUTH_TOKEN", "tok-1")]
    cr._credential_check()
    assert "claude.probe.api-key" not in cr._HEALTH
    assert "api-key" not in cr._PROBES
