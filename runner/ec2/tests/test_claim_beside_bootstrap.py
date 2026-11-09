"""A restart does not stop a box taking work.

A refresh must not make the box run its whole bootstrap before asking for work: a
message sent just after would wait minutes while its health says "claiming".
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest
from _bash import BASH, _fn


class _Enrollment:
    def join(self, timeout=None):
        return None


def _main_world(mod, monkeypatch, tmp_path):
    """`main()` up to the claim loop, recording the order things happen in."""
    order: list[str] = []
    monkeypatch.setattr(mod, "BASE_URL", "https://canopy.test")
    monkeypatch.setattr(mod, "BOOTSTRAPPED_MARKER", tmp_path / "bootstrapped")
    monkeypatch.setattr(mod, "SETUP_HOLD_FILE", tmp_path / "hold")
    monkeypatch.setattr(mod, "RUNNER_INSTANCE", "")
    monkeypatch.setattr(mod, "RUNNER_SRC_DIR", str(tmp_path / "no-clone"))
    monkeypatch.setattr(mod, "pair_or_load", lambda: "rid")
    monkeypatch.setattr(mod, "adopt_turns", lambda rid: {})
    monkeypatch.setattr(mod, "sweep_turn_envs", lambda: 0)
    monkeypatch.setattr(mod, "_clear_stale_connection_tokens", lambda: None)
    monkeypatch.setattr(mod, "sweep_kept_conversations", lambda rid: None)
    monkeypatch.setattr(mod, "sensitive_data_sweep_leftovers", lambda: None)
    monkeypatch.setattr(mod, "start_aws_device_key_enrollment", lambda rid: _Enrollment())
    monkeypatch.setattr(mod, "_retire_shared_github_credentials", lambda: None)
    monkeypatch.setattr(mod, "_probe_all", lambda labels=None: order.append("probe"))
    monkeypatch.setattr(mod, "fetch_and_stage_credential",
                        lambda rid, probe=True: order.append(f"stage probe={probe}") or True)
    monkeypatch.setattr(mod, "run_over_ws",
                        lambda rid: order.append(f"claim loop, hold={mod.setting_up()!r}") or True)
    return order


def test_a_box_set_up_before_claims_while_its_bootstrap_is_still_running(cloud_runner, monkeypatch, tmp_path):
    order = _main_world(cloud_runner, monkeypatch, tmp_path)
    (tmp_path / "bootstrapped").write_text("1\n")
    release = threading.Event()
    started = threading.Event()

    def slow_bootstrap():
        started.set()
        release.wait(5)
        order.append("bootstrap finished")

    monkeypatch.setattr(cloud_runner, "bootstrap_agent_fleet", slow_bootstrap)
    cloud_runner.main()
    assert started.wait(5)
    # The stage asked for no probe; the loop began with the pass unfinished and no hold.
    assert order[0] == "stage probe=False"
    assert "claim loop, hold=''" in order and "bootstrap finished" not in order
    assert cloud_runner.bootstrap_running()
    release.set()
    assert cloud_runner._BOOTSTRAP_DONE.wait(5)
    assert order.index("probe") < order.index("bootstrap finished")


def test_a_new_box_bootstraps_before_it_claims(cloud_runner, monkeypatch, tmp_path):
    order = _main_world(cloud_runner, monkeypatch, tmp_path)
    monkeypatch.setattr(cloud_runner, "bootstrap_agent_fleet",
                        lambda: order.append(f"bootstrap, hold={cloud_runner.setting_up()!r}"))
    cloud_runner.main()
    assert order == ["stage probe=True",
                     "bootstrap, hold='setting up for the first time'",
                     "claim loop, hold=''"]


def test_the_switch_puts_the_bootstrap_first_again(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "BOOTSTRAPPED_MARKER", tmp_path / "bootstrapped")
    (tmp_path / "bootstrapped").write_text("1\n")
    assert cloud_runner.may_bootstrap_beside_claims()
    monkeypatch.setenv("BOOTSTRAP_BESIDE_CLAIMS", "0")
    assert not cloud_runner.may_bootstrap_beside_claims()


def test_a_failing_pass_does_not_take_the_claim_loop_with_it(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "_probe_all", lambda: None)
    monkeypatch.setattr(cloud_runner, "bootstrap_agent_fleet", lambda: 1 / 0)
    cloud_runner.start_bootstrap_beside_claims(_Enrollment()).join(5)
    assert not cloud_runner.bootstrap_running()


# ---- what holds a claim ----------------------------------------------------------------

@pytest.fixture
def hold(cloud_runner, monkeypatch, tmp_path):
    path = tmp_path / "hold"
    monkeypatch.setattr(cloud_runner, "SETUP_HOLD_FILE", path)
    monkeypatch.setattr(cloud_runner, "_memory_headroom_ok", lambda: True)
    return path


def test_no_claim_while_a_tool_is_being_replaced_and_health_says_why(cloud_runner, hold):
    assert cloud_runner.claim_gate()[0] is True
    hold.write_text("tools\n")
    claiming, reason, detail = cloud_runner.claim_gate()
    assert (claiming, reason) == (False, "setup")
    assert "installing updated tools" in detail
    assert cloud_runner.health_report()["setting_up"] == "installing updated tools"
    hold.unlink()
    assert cloud_runner.claim_gate()[0] is True
    assert cloud_runner.health_report()["setting_up"] == ""


def test_preparing_one_agent_holds_only_that_agents_turns(cloud_runner, hold):
    hold.write_text("agent:ace\n")
    assert cloud_runner.claim_gate()[0] is True
    assert cloud_runner.setting_up() == ""
    assert cloud_runner.backed_off_slugs() == ["ace"]


def test_a_killed_scripts_hold_file_expires(cloud_runner, hold):
    hold.write_text("tools\n")
    old = time.time() - cloud_runner.SETUP_HOLD_MAX_SECONDS - 1
    os.utime(hold, (old, old))
    assert cloud_runner.claim_gate()[0] is True


def test_no_claim_during_the_checkout_of_the_clone_turns_import_from(cloud_runner, monkeypatch, hold):
    seen = []
    monkeypatch.setattr(cloud_runner, "ensure_updater_clone", lambda: True)
    monkeypatch.setattr(cloud_runner, "sync_runner_src",
                        lambda: seen.append(cloud_runner.setting_up()) or False)
    cloud_runner._run_bootstrap()
    assert seen == ["updating its code"]
    assert cloud_runner.setting_up() == ""


# ---- what must wait for the pass ------------------------------------------------------

def test_a_refresh_due_mid_pass_waits_for_it(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "IN_FLIGHT_FILE", tmp_path / "in-flight")
    cloud_runner._BOOTSTRAP_DONE.clear()
    assert cloud_runner.maybe_self_refresh(True) is False
    assert not (tmp_path / "last-self-refresh").exists(), "spent the floor on a refresh it did not do"


def test_a_turn_for_an_agent_with_no_clone_waits_for_the_pass(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    ran = []
    monkeypatch.setattr(cloud_runner, "_LAZY_CLONER", lambda slug, env: ran.append("clone"))
    monkeypatch.setattr(cloud_runner, "_LAZY_BOOTSTRAPPER", lambda slug, env: ran.append("prepare"))
    cloud_runner._BOOTSTRAP_DONE.clear()
    turn = threading.Thread(target=cloud_runner._ensure_agent_clone, args=("newbie",), daemon=True)
    turn.start()
    turn.join(0.2)
    assert turn.is_alive() and ran == []
    cloud_runner._BOOTSTRAP_DONE.set()
    turn.join(5)
    assert ran == ["clone", "prepare"]


def test_a_turn_for_an_agent_on_the_box_does_not_wait(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "AGENT_ROOT", str(tmp_path))
    (tmp_path / "ace" / ".git").mkdir(parents=True)
    cloud_runner._BOOTSTRAP_DONE.clear()
    done = threading.Thread(target=cloud_runner._ensure_agent_clone, args=("ace",), daemon=True)
    done.start()
    done.join(5)
    assert not done.is_alive()


# ---- leaving ---------------------------------------------------------------------------

def test_a_daemon_that_leaves_takes_its_whole_script_with_it(cloud_runner, tmp_path):
    grandchild_pid = tmp_path / "pid"
    got = {}

    def run():
        got["rc"] = cloud_runner._run_teed(
            ["bash", "-c", f"sleep 60 & echo $! > {grandchild_pid}; wait"],
            env=dict(os.environ), timeout=30,
            on_start=lambda proc: cloud_runner._BOOTSTRAP_PROC.update(proc=proc))[0]

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    deadline = time.time() + 5
    while time.time() < deadline and not (grandchild_pid.exists() and grandchild_pid.read_text().strip()):
        time.sleep(0.02)
    cloud_runner.stop_bootstrap()
    worker.join(5)
    assert not worker.is_alive(), "the output pipe stayed open: a child outlived the script"
    assert got["rc"] != 0
    pid = int(grandchild_pid.read_text())
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.02)
    pytest.fail("the script's child outlived it")


def test_a_pass_cut_short_by_leaving_does_not_read_as_a_finished_one(cloud_runner, monkeypatch, tmp_path):
    marker = tmp_path / "bootstrapped"
    monkeypatch.setattr(cloud_runner, "BOOTSTRAPPED_MARKER", marker)
    monkeypatch.setattr(cloud_runner, "_RUNNER_ID", "")
    monkeypatch.setattr(cloud_runner, "_github_readiness", lambda: None)
    monkeypatch.setattr(cloud_runner, "_install_repo_worktree_shim", lambda: None)
    monkeypatch.setattr(cloud_runner, "_guard_shared_clone", lambda p: None)

    def leave_midway():
        cloud_runner._stop = True

    monkeypatch.setattr(cloud_runner, "_run_bootstrap", leave_midway)
    cloud_runner.bootstrap_agent_fleet()
    assert not marker.exists() and cloud_runner._BOOTSTRAPPED_AT == 0.0


def test_a_pass_beside_turns_runs_at_low_priority(cloud_runner, monkeypatch, tmp_path):
    script = tmp_path / "src" / "runner" / "ec2" / "bootstrap_agents.sh"
    script.parent.mkdir(parents=True)
    script.write_text("")
    monkeypatch.setattr(cloud_runner, "RUNNER_SRC_DIR", str(tmp_path / "src"))
    monkeypatch.setattr(cloud_runner, "ensure_updater_clone", lambda: True)
    monkeypatch.setattr(cloud_runner, "sync_runner_src", lambda: True)
    monkeypatch.setattr(cloud_runner, "_bearer", lambda **kw: "t")
    monkeypatch.setattr(cloud_runner, "_low_priority_prefix", lambda: ["nice"])
    seen = []
    monkeypatch.setattr(cloud_runner, "_run_teed",
                        lambda cmd, env, timeout, **kw: seen.append((cmd, env)) or (0, []))
    cloud_runner._run_bootstrap()
    cloud_runner._BOOTSTRAP_BESIDE = True
    cloud_runner._run_bootstrap()
    assert seen[0][0] == ["bash", str(script)]
    assert seen[1][0] == ["nice", "bash", str(script)]
    assert seen[1][1]["CANOPY_SETUP_HOLD_FILE"] == str(cloud_runner.SETUP_HOLD_FILE)


# ---- the script's half -----------------------------------------------------------------

@pytest.mark.skipif(BASH is None, reason="no modern bash")
def test_the_script_holds_claims_only_while_it_replaces_a_tool(tmp_path):
    home = tmp_path / "home"
    clone = home / ".claude/plugins/marketplaces/canopy"
    clone.mkdir(parents=True)
    (clone / "pyproject.toml").write_text('version = "2.0.0"\n')
    binv = tmp_path / "bin"
    binv.mkdir()
    hold, during = tmp_path / "hold", tmp_path / "during"
    (binv / "canopy").write_text("#!/bin/sh\necho 'canopy 1.0.0'\n")
    (binv / "uv").write_text(f"#!/bin/sh\ncat {hold} > {during}\n")
    for f in ("canopy", "uv"):
        (binv / f).chmod(0o755)
    script = "\n".join(["set -uo pipefail", "ok() { :; }", "warn() { :; }", "log() { :; }",
                        _fn("hold_claims"), _fn("release_claims"),
                        _fn("reinstall_cli_from_marketplace_clone"),
                        "reinstall_cli_from_marketplace_clone"])
    subprocess.run([BASH, "-c", script], check=True,
                   env={"HOME": str(home), "PATH": f"{binv}:/usr/bin:/bin",
                        "CANOPY_SETUP_HOLD_FILE": str(hold)})
    assert during.read_text().strip() == "tools"
    assert not hold.exists()


@pytest.mark.skipif(BASH is None, reason="no modern bash")
def test_the_timer_pass_holds_nothing(tmp_path):
    script = "\n".join(["set -uo pipefail", _fn("hold_claims"), _fn("release_claims"),
                        "hold_claims tools", "release_claims"])
    proc = subprocess.run([BASH, "-c", script], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
    assert proc.returncode == 0 and not proc.stderr
