"""The box reports its own features, keeps its package clone to itself, and
refreshes itself when asked or overdue — never mid-turn, never in a loop.

All three come from one incident (cloud-ec2-1, 2026-09-22): an agent turn left
the shared canopy-web clone on a branch, `git pull --ff-only` failed on the next
start, the three in-repo packages were never exposed, and the box ran six hours
online + ready with no transcripts, no inbox and no ACP.
"""
from __future__ import annotations

import subprocess

import pytest


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path):
    """A bare-ish origin with main carrying the two in-repo packages."""
    src = tmp_path / "origin"
    for parent, name in (("packages", "canopy_transcript"), ("runner", "canopy_acp"),
                         ("runner", "canopy_runner")):
        pkg = src / parent / name / name
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
    _git("init", "-q", "-b", "main", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=src)
    return src


def _point_at(cr, monkeypatch, origin, dest):
    monkeypatch.setattr(cr, "CANOPY_WEB_REPO_URL", str(origin))
    monkeypatch.setattr(cr, "RUNNER_SRC_DIR", str(dest))
    monkeypatch.setattr(cr.sys, "path", list(cr.sys.path))
    monkeypatch.setattr(cr, "_install_acp_adapter", lambda: None)


def test_a_branch_left_in_the_clone_no_longer_disables_the_packages(
        cloud_runner, monkeypatch, origin, tmp_path):
    dest = tmp_path / "src"
    _point_at(cloud_runner, monkeypatch, origin, dest)
    assert cloud_runner.sync_runner_src() is True
    # Exactly what an agent turn did to /opt/canopy-web: a local branch with a
    # commit origin does not have. `pull --ff-only` refused this.
    _git("checkout", "-qb", "docs/some-spec", cwd=dest)
    (dest / "spec.md").write_text("x")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=dest)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "spec", cwd=dest)

    assert cloud_runner.sync_runner_src() is True
    assert not (dest / "spec.md").exists()  # reset to origin/main, not merged
    assert str(dest / "packages" / "canopy_transcript") in cloud_runner.sys.path
    assert cloud_runner._HEALTH["code_clone"]["status"] == "ok"


def test_an_unreachable_origin_still_exposes_the_clone_it_has(
        cloud_runner, monkeypatch, origin, tmp_path):
    dest = tmp_path / "src"
    _point_at(cloud_runner, monkeypatch, origin, dest)
    cloud_runner.sync_runner_src()
    monkeypatch.setattr(cloud_runner, "CANOPY_WEB_REPO_URL", str(tmp_path / "gone"))
    cloud_runner.sys.path[:] = [p for p in cloud_runner.sys.path if str(dest) not in p]

    assert cloud_runner.sync_runner_src() is True
    assert str(dest / "packages" / "canopy_transcript") in cloud_runner.sys.path
    check = cloud_runner._HEALTH["code_clone"]
    assert check["status"] == "warn" and "using the clone as it was" in check["detail"]


def test_a_package_that_does_not_import_is_a_failed_check(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_transcript_core", lambda: None)
    monkeypatch.setattr(cloud_runner, "_inbox_core", lambda: object())
    monkeypatch.setattr(cloud_runner, "_acp_core", lambda: None)
    monkeypatch.setattr(cloud_runner, "RUNNER_EXECUTOR", "acp")
    monkeypatch.setattr(cloud_runner, "_BOOTSTRAPPED_AT", 1.0)
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    report = cloud_runner.health_report()
    by = {c["name"]: c for c in report["checks"]}
    assert by["packages.transcripts"]["status"] == "fail"
    assert by["packages.inbox"]["status"] == "ok"
    assert by["packages.acp"]["status"] == "fail"


def test_packages_are_not_judged_before_bootstrap(cloud_runner, monkeypatch):
    # They are exposed BY bootstrap; failing them earlier is a false alarm on
    # every start.
    monkeypatch.setattr(cloud_runner, "_transcript_core", lambda: None)
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    names = {c["name"] for c in cloud_runner.health_report()["checks"]}
    assert "packages.transcripts" not in names


@pytest.mark.parametrize("n,status", [(0, "fail"), (1, "warn"), (2, "ok")])
def test_credential_depth(cloud_runner, monkeypatch, n, status):
    monkeypatch.setattr(cloud_runner, "_CLAUDE_CREDS", [("x", "V", "v")] * n)
    monkeypatch.setattr(cloud_runner, "_slow_checks", lambda: None)
    by = {c["name"]: c for c in cloud_runner.health_report()["checks"]}
    assert by["claude.credentials"]["status"] == status


def test_cli_behind_the_plugin_is_a_warning(cloud_runner, monkeypatch):
    versions = {"claude": "2.1.266", "canopy": "0.2.506"}
    monkeypatch.setattr(cloud_runner, "_version_of", lambda cmd: versions.get(pathlib_name(cmd[0]), ""))
    monkeypatch.setattr(cloud_runner, "_marketplace_canopy_version", lambda: "0.2.509")
    cloud_runner._slow_checks()
    assert cloud_runner._HEALTH["canopy.cli"]["status"] == "warn"
    assert "0.2.509" in cloud_runner._HEALTH["canopy.cli"]["detail"]
    assert cloud_runner._HEALTH["claude.version"]["status"] == "ok"


def pathlib_name(cmd0: str) -> str:
    return cmd0.rsplit("/", 1)[-1]


def test_a_throwing_check_never_costs_the_beat(cloud_runner, monkeypatch):
    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(cloud_runner, "_credential_check", boom)
    body = cloud_runner._heartbeat_body([])
    assert "health" in body and "checks" in body["health"]


# ── self-refresh ─────────────────────────────────────────────────────────────

@pytest.fixture
def refresh(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner, "RUNNER_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(cloud_runner, "_in_flight_ids", lambda: [])
    monkeypatch.setattr(cloud_runner, "_BOOTSTRAPPED_AT", 1000.0)
    return cloud_runner, calls


def test_an_operator_request_restarts_an_idle_box(refresh):
    cr, calls = refresh
    assert cr.maybe_self_refresh(True, now=2000.0) is True
    assert calls == [["sudo", "-n", "systemctl", "restart", "canopy-runner.service"]]


def test_never_mid_turn(refresh, monkeypatch):
    cr, calls = refresh
    monkeypatch.setattr(cr, "_in_flight_ids", lambda: ["t1"])
    assert cr.maybe_self_refresh(True, now=2000.0) is False and calls == []


def test_no_request_and_a_fresh_bootstrap_does_nothing(refresh):
    cr, calls = refresh
    assert cr.maybe_self_refresh(False, now=2000.0) is False and calls == []


def test_an_overdue_bootstrap_refreshes_on_its_own(refresh):
    cr, calls = refresh
    assert cr.maybe_self_refresh(False, now=1000.0 + cr.REFRESH_MAX_AGE_SECONDS) is True


def test_the_floor_between_restarts_survives_a_restart(refresh):
    # A bootstrap that never completes must not become a restart loop: the
    # guard is a FILE, so a freshly started process still sees it.
    cr, calls = refresh
    assert cr.maybe_self_refresh(True, now=2000.0) is True
    assert cr.maybe_self_refresh(True, now=2000.0 + 60) is False
    assert cr.maybe_self_refresh(True, now=2000.0 + cr.REFRESH_MIN_INTERVAL_SECONDS) is True
    assert len(calls) == 2


def test_a_failed_bootstrap_says_why_in_the_check(cloud_runner, monkeypatch, tmp_path, capsys):
    # "exited 1" alone sent someone to journald on 2026-09-22; the reason was the
    # script's last line (`line 1024: ace: unbound variable`).
    src = tmp_path / "src"
    script = src / "runner" / "ec2" / "bootstrap_agents.sh"
    script.parent.mkdir(parents=True)
    script.write_text("echo 'step 3: agents'\necho 'line 1024: ace: unbound variable' >&2\nexit 1\n")
    monkeypatch.setattr(cloud_runner, "RUNNER_SRC_DIR", str(src))
    monkeypatch.setattr(cloud_runner, "sync_runner_src", lambda: True)
    cloud_runner._run_bootstrap()
    check = cloud_runner._HEALTH["bootstrap"]
    assert check["status"] == "fail"
    assert "ace: unbound variable" in check["detail"]
    assert "step 3: agents" in capsys.readouterr().out  # still reaches journald


def test_a_silent_hang_is_still_killed(cloud_runner):
    rc, tail = cloud_runner._run_teed(["bash", "-c", "sleep 30"], env=dict(cloud_runner.os.environ), timeout=0.5)
    assert rc != 0 and tail == ["killed after 0s"]


# ── a turn carries its OWN 1Password key, never a box-wide one ───────────────
# Agent.op_vault exists so a compromise is bounded to one agent. Staging a
# box-wide key into this process's env handed every turn a credential that reads
# every vault — the boundary undone by inheritance (2026-09-22).

def test_a_turn_gets_its_own_op_key_not_the_boxs(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "BOX-WIDE-KEY")
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (200, {"op_sa_token": "HAL-KEY"}))
    assert cloud_runner._agent_env("hal")["OP_SERVICE_ACCOUNT_TOKEN"] == "HAL-KEY"


def test_an_unregistered_agent_gets_no_op_key_at_all(cloud_runner, monkeypatch, tmp_path):
    # Not the box's. `op` is simply unavailable, which is the honest state and
    # says so in the log.
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "BOX-WIDE-KEY")
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_api", lambda m, p, *a, **k: (200, {"op_sa_token": ""}))
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in cloud_runner._agent_env("ada")


def test_the_key_is_cached_but_not_forever(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(cloud_runner, "_AGENT_OP", {})
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (calls.append(p), (200, {"op_sa_token": "K"}))[1])
    cloud_runner._agent_env("hal"); cloud_runner._agent_env("hal")
    assert len(calls) == 1
    cloud_runner._AGENT_OP["hal"] = (0.0, "K")   # expired
    cloud_runner._agent_env("hal")
    assert len(calls) == 2


def test_a_project_turn_carries_no_agent_key(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: pytest.fail("must not resolve"))
    assert cloud_runner._agent_env(None) is not None
