"""A turn reads its agent's vault as it is NOW, not as it was at the last bootstrap.

2026-10-12: Jonathan changed ACE's Drive root in 1Password. cloud-ec2-2 kept the
old value all day, because `~/.ace/.env` was written only by bootstrap (once per
service start) and every turn trusted that copy. `_agent_env` now checks the
agent's vault when a turn is claimed, at most once per TTL — through canopy's
`canopy cred sync`, the same command an agent session runs at start (re-inject only
when the vault or template changed; that logic and its tests live in canopy's
cred_cli). What is pinned HERE is the runner's half: the throttle, the key and vault it
hands over, and that a failure never costs the turn.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess

import pytest


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    """A box with ACE bootstrapped (a clone holding `.env.tpl`, an old `.env`) and a stub
    `canopy cred sync` that writes the vault's current value, or fails."""
    home = tmp_path / "home"
    root = tmp_path / "agents"
    (root / "ace").mkdir(parents=True)
    (root / "ace" / ".env.tpl").write_text("GDRIVE_ROOT_FOLDER=op://${AGENT_VAULT}/drive/root\n")
    (home / ".ace").mkdir(parents=True)
    (home / ".ace" / ".env").write_text("GDRIVE_ROOT_FOLDER=old-root\n")
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: home)
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(root))
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_AGENT_ENV_AT", {})
    monkeypatch.setattr(cloud_runner, "_canopy_cli", lambda: "/u/.local/bin/canopy")
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "ACE-KEY",
                                                     "op_vault": "Agent-Ace"}))
    state = {"calls": [], "value": "new-root", "rc": 0, "stderr": "", "raise": None}

    def fake_run(argv, *, env, **kw):
        state["calls"].append((argv, env, kw.get("cwd")))
        if state["raise"]:
            raise state["raise"]
        if state["rc"] == 0:
            (home / ".ace" / ".env").write_text(f"GDRIVE_ROOT_FOLDER={state['value']}\n")
            return subprocess.CompletedProcess(argv, 0, "", "[canopy cred] ace: re-injected")
        return subprocess.CompletedProcess(argv, state["rc"], "", state["stderr"])

    monkeypatch.setattr(cloud_runner.subprocess, "run", fake_run)
    state["home"] = home
    return state


def _check(cloud_runner, slug="ace"):
    return cloud_runner._HEALTH.get(f"env.{slug}")


def test_a_vault_change_reaches_the_next_turn_without_a_restart(cloud_runner, box):
    env = cloud_runner._agent_env("ace")
    assert env["GDRIVE_ROOT_FOLDER"] == "new-root"
    assert _check(cloud_runner)["status"] == "ok" and "re-injected" in _check(cloud_runner)["detail"]


def test_it_is_canopy_cred_sync_with_the_agents_own_key_and_vault(cloud_runner, box, monkeypatch):
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "BOX-WIDE-KEY")
    monkeypatch.setenv("OP_SESSION_dimagi", "personal-session")
    monkeypatch.setenv("OP_ACCOUNT", "dimagi.1password.com")
    cloud_runner._agent_env("ace")
    (argv, env, cwd), = box["calls"]
    assert argv == ["/u/.local/bin/canopy", "cred", "sync", "--agent", "ace", "--ttl", "0"]
    assert (env["OP_SERVICE_ACCOUNT_TOKEN"], env["AGENT_VAULT"], env["AGENT_SLUG"]) == (
        "ACE-KEY", "Agent-Ace", "ace")
    assert "OP_SESSION_dimagi" not in env and "OP_ACCOUNT" not in env
    assert cwd.endswith("/agents/ace")          # where cred sync finds the template


def test_a_burst_of_claims_refreshes_once_per_ttl(cloud_runner, box):
    cloud_runner._agent_env("ace"); cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 1
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0      # TTL elapsed
    cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 2


@pytest.mark.parametrize("failure", [
    {"rc": 1, "stderr": '[canopy cred] ace: op inject failed: could not resolve "op://Agent-Ace/drive/root"'},
    {"raise": subprocess.TimeoutExpired(["canopy"], 30)},
    {"raise": FileNotFoundError("canopy")},
])
def test_a_failed_refresh_keeps_the_last_good_file_and_says_so(cloud_runner, box, failure):
    box.update(failure)
    env = cloud_runner._agent_env("ace")
    assert env["GDRIVE_ROOT_FOLDER"] == "old-root"      # the turn still runs
    check = _check(cloud_runner)
    assert check["status"] == "warn" and "last good" in check["detail"]


def test_a_failed_refresh_is_not_retried_on_every_claim(cloud_runner, box):
    box["raise"] = subprocess.TimeoutExpired(["canopy"], 30)
    cloud_runner._agent_env("ace"); cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 1


def test_no_key_or_no_template_means_no_refresh(cloud_runner, box, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": ""}))
    cloud_runner._agent_env("ace")
    assert box["calls"] == []
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": "K"}))
    cloud_runner._AGENT_OP.clear()
    cloud_runner._agent_env("eva")                    # no clone, so no .env.tpl
    assert box["calls"] == []


def test_the_turn_carries_the_instances_recorded_vault(cloud_runner, box, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "ACE-KEY", "op_vault": "Ace-Prod"}))
    env = cloud_runner._agent_env("ace")
    assert box["calls"][0][1]["AGENT_VAULT"] == "Ace-Prod"
    assert env["AGENT_VAULT"] == "Ace-Prod" and env["AGENT_SLUG"] == "ace"


def test_no_recorded_vault_refreshes_nothing_and_says_so(cloud_runner, box, monkeypatch):
    """Never derive `Agent-<Slug>`: a key with no recorded vault is unconfigured."""
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "ACE-KEY", "op_vault": ""}))
    env = cloud_runner._agent_env("ace")
    assert box["calls"] == []
    assert env["GDRIVE_ROOT_FOLDER"] == "old-root" and "AGENT_VAULT" not in env
    check = _check(cloud_runner)
    assert check["status"] == "warn" and "no 1Password vault" in check["detail"]
