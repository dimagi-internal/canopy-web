"""A turn reads its agent's vault as it is NOW, not as it was at the last bootstrap.

2026-10-12: Jonathan changed ACE's Drive root in 1Password. cloud-ec2-2 kept the
old value all day, because `~/.ace/.env` was written only by bootstrap (once per
service start) and every turn trusted that copy. `_agent_env` now checks the
agent's vault when a turn is claimed — one cheap `op vault get` per TTL — and
re-injects the file only when the vault (or the template) changed. Never at the
cost of the turn.
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
    state = {"calls": [], "value": "new-root", "rc": 0, "stderr": "", "raise": None,
             "version": 7, "probe_rc": 0, "probe_stderr": ""}

    def fake_run(argv, *, env, **kw):
        state["calls"].append((argv, env))
        if state["raise"]:
            raise state["raise"]
        if argv[:3] == ["op", "vault", "get"]:
            out = json.dumps({"name": argv[3], "content_version": state["version"]})
            return subprocess.CompletedProcess(argv, state["probe_rc"],
                                               out if state["probe_rc"] == 0 else "",
                                               state["probe_stderr"])
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


def _injects(box):
    return [argv for argv, _ in box["calls"] if argv[:2] == ["op", "inject"]]


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
    assert [a[:3] for a, _ in box["calls"]] == [["op", "vault", "get"], ["op", "inject", "-i"]]
    assert box["calls"][0][0][3] == "Agent-Ace"          # the vault the template names
    for _, env in box["calls"]:
        assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "ACE-KEY"
    env = box["calls"][1][1]
    assert "OP_SESSION_dimagi" not in env and "OP_ACCOUNT" not in env


def test_the_refreshed_file_is_private(cloud_runner, box):
    cloud_runner._agent_env("ace")
    mode = stat.S_IMODE(os.stat(box["home"] / ".ace" / ".env").st_mode)
    assert mode == 0o600


def test_a_burst_of_claims_touches_1password_once_per_ttl(cloud_runner, box):
    cloud_runner._agent_env("ace"); cloud_runner._agent_env("ace")
    assert len(box["calls"]) == 2        # one probe, one inject
    assert len(_injects(box)) == 1


def test_an_unchanged_vault_costs_one_probe_and_no_inject(cloud_runner, box):
    cloud_runner._agent_env("ace")
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0   # TTL elapsed
    cloud_runner._agent_env("ace")
    assert len(_injects(box)) == 1 and len(box["calls"]) == 3
    assert "matches the vault" in _check(cloud_runner)["detail"]


def test_a_vault_edit_moves_the_version_and_re_injects(cloud_runner, box):
    cloud_runner._agent_env("ace")
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0
    box["version"], box["value"] = 8, "newer-root"
    assert cloud_runner._agent_env("ace")["GDRIVE_ROOT_FOLDER"] == "newer-root"
    assert len(_injects(box)) == 2


def test_a_template_edit_re_injects_even_if_the_vault_did_not_move(cloud_runner, box, tmp_path):
    cloud_runner._agent_env("ace")
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0
    tpl = tmp_path / "agents" / "ace" / ".env.tpl"
    tpl.write_text(tpl.read_text() + "CANOPY_WEB_PAT=op://Agent-Ace/canopy-pat/credential\n")
    cloud_runner._agent_env("ace")
    assert len(_injects(box)) == 2


def test_a_key_from_the_wrong_account_says_which_account(cloud_runner, box, monkeypatch):
    header = base64.urlsafe_b64encode(
        json.dumps({"signInAddress": "someone-else.1password.com"}).encode()).decode().rstrip("=")
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "ops_" + header}))
    box["probe_rc"] = 1
    box["probe_stderr"] = "[ERROR] Signin credentials are not compatible with the provided user auth from server"
    env = cloud_runner._agent_env("ace")
    assert env["GDRIVE_ROOT_FOLDER"] == "old-root" and _injects(box) == []
    assert "signs in to someone-else.1password.com" in _check(cloud_runner)["detail"]


@pytest.mark.parametrize("failure", [
    {"rc": 1, "stderr": '[ERROR] could not resolve "op://Agent-Ace/drive/root"'},
    {"probe_rc": 1, "probe_stderr": '[ERROR] "Agent-Ace" isn\'t a vault in this account'},
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


def test_every_check_is_stamped_for_the_sessions_own_hook(cloud_runner, box):
    # The agent session's SessionStart hook runs `canopy cred sync`, which skips when
    # `checked_at` is fresh — so a cloud turn is probed once, by the runner.
    cloud_runner._agent_env("ace")
    stamp = json.loads((box["home"] / ".ace" / ".env.stamp").read_text())
    assert stamp["fingerprint"] and stamp["checked_at"] == stamp["injected_at"]
    cloud_runner._AGENT_ENV_AT["ace"] = 0.0
    box["raise"] = subprocess.TimeoutExpired(["op"], 30)
    cloud_runner._agent_env("ace")
    after = json.loads((box["home"] / ".ace" / ".env.stamp").read_text())
    assert after["fingerprint"] == stamp["fingerprint"] and after["checked_at"] >= stamp["checked_at"]
