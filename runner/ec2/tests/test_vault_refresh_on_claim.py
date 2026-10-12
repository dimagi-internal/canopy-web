"""A turn reads its agent's vault as it is NOW, not as it was at the last bootstrap.

2026-10-12: Jonathan changed ACE's Drive root in 1Password. cloud-ec2-2 kept the
old value all day, because `~/.ace/.env` was written only by bootstrap (once per
service start) and every turn trusted that copy. `_agent_env` now re-injects the
file from the agent's vault when a turn is claimed — rate-limited, and never at
the cost of the turn.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    """A box with ACE bootstrapped: a clone holding `.env.tpl`, an old `.env`,
    and a stub `op` that records how it was called."""
    home = tmp_path / "home"
    root = tmp_path / "agents"
    (root / "ace").mkdir(parents=True)
    (root / "ace" / ".env.tpl").write_text("GDRIVE_ROOT_FOLDER=op://Agent-Ace/drive/root\n")
    (home / ".ace").mkdir(parents=True)
    (home / ".ace" / ".env").write_text("GDRIVE_ROOT_FOLDER=old-root\n")
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: home)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(root))
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_AGENT_ENV_AT", {})
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "ACE-KEY"}))
    state = {"calls": [], "value": "new-root", "rc": 0, "stderr": "", "raise": None}

    def fake_run(argv, *, env, **kw):
        state["calls"].append((argv, env))
        if state["raise"]:
            raise state["raise"]
        if state["rc"] == 0:
            out = argv[argv.index("-o") + 1]
            with open(out, "w") as fh:
                fh.write(f"GDRIVE_ROOT_FOLDER={state['value']}\n")
        return subprocess.CompletedProcess(argv, state["rc"], "", state["stderr"])

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    state["home"] = home
    return state


def _check(cloud_runner, slug="ace"):
    return cloud_runner._HEALTH.get(f"env.{slug}")


def test_a_vault_change_reaches_the_next_turn_without_a_restart(cloud_runner, box):
    env = cloud_runner._agent_env("ace")
    assert env["GDRIVE_ROOT_FOLDER"] == "new-root"
    assert (box["home"] / ".ace" / ".env").read_text() == "GDRIVE_ROOT_FOLDER=new-root\n"
    assert _check(cloud_runner)["status"] == "ok"


def test_the_inject_uses_the_agents_own_key_and_no_personal_signin(cloud_runner, box, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "BOX-WIDE-KEY")
    monkeypatch.setenv("OP_SESSION_dimagi", "personal-session")
    monkeypatch.setenv("OP_ACCOUNT", "dimagi.1password.com")
    cloud_runner._agent_env("ace")
    argv, env = box["calls"][0]
    assert argv[:2] == ["op", "inject"]
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ACE-KEY"
    assert "OP_SESSION_dimagi" not in env and "OP_ACCOUNT" not in env


def test_the_refreshed_file_is_private(cloud_runner, box):
    cloud_runner._agent_env("ace")
    mode = stat.S_IMODE(os.stat(box["home"] / ".ace" / ".env").st_mode)
    assert mode == 0o600


def test_a_burst_of_claims_injects_once_per_ttl(cloud_runner, box):
    cloud_runner._agent_env("ace"); cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 1
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0   # TTL elapsed
    box["value"] = "newer-root"
    assert cloud_runner._agent_env("ace")["GDRIVE_ROOT_FOLDER"] == "newer-root"
    assert len(box["calls"]) == 2


@pytest.mark.parametrize("failure", [
    {"rc": 1, "stderr": '[ERROR] could not resolve "op://Agent-Ace/drive/root"'},
    {"raise": subprocess.TimeoutExpired(["op"], 30)},
    {"raise": FileNotFoundError("op")},
])
def test_a_failed_refresh_keeps_the_last_good_file_and_says_so(cloud_runner, box, failure):
    box.update(failure)
    env = cloud_runner._agent_env("ace")
    assert env["GDRIVE_ROOT_FOLDER"] == "old-root"      # the turn still runs
    assert not (box["home"] / ".ace" / ".env.refresh").exists()
    check = _check(cloud_runner)
    assert check["status"] == "warn" and "last good" in check["detail"]


def test_a_failed_refresh_is_not_retried_on_every_claim(cloud_runner, box):
    box["raise"] = subprocess.TimeoutExpired(["op"], 30)
    cloud_runner._agent_env("ace"); cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 1


def test_no_key_or_no_template_means_no_inject(cloud_runner, box, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": ""}))
    cloud_runner._agent_env("ace")
    assert box["calls"] == []
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": "K"}))
    cloud_runner._AGENT_OP.clear()
    cloud_runner._agent_env("eva")                    # no clone, so no .env.tpl
    assert box["calls"] == []
