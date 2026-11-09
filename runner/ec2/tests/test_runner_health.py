"""The box reports its own features, keeps its package clone to itself, and
refreshes itself when asked or overdue — never mid-turn, never in a loop.

All three guard one failure: an agent turn leaves the shared canopy-web clone on a
branch, `git pull --ff-only` fails on the next start, the three in-repo packages
are never exposed, and the box runs online + ready with no transcripts, no inbox
and no ACP.
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


def _point_at(cr, monkeypatch, origin, dest, *, stamp_sha: str | None = None):
    monkeypatch.setattr(cr, "CANOPY_WEB_REPO_URL", str(origin))
    monkeypatch.setattr(cr, "RUNNER_SRC_DIR", str(dest))
    monkeypatch.setattr(cr.sys, "path", list(cr.sys.path))
    monkeypatch.setattr(cr, "_install_acp_adapter", lambda: None)
    stamp = dest.parent / "build-info.json"
    if stamp_sha is not None:
        stamp.write_text(cr.json.dumps({"sha": stamp_sha, "committed_at": 1}))
    monkeypatch.setattr(cr, "BUILD_INFO_FILE", stamp)
    monkeypatch.setattr(cr, "_BUILD_INFO", None)


def _commit(src, name, body="x"):
    (src / name).write_text(body)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=src)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name, cwd=src)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, check=True,
                          capture_output=True, text=True).stdout.strip()


def _head(dest):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, check=True,
                          capture_output=True, text=True).stdout.strip()


# ── the clone is the DEPLOYED commit, not whatever main is at restart ─────────

def test_the_clone_is_pinned_to_the_deployed_sha_not_main(
        cloud_runner, monkeypatch, origin, tmp_path):
    deployed = _commit(origin, "deployed.txt")
    _commit(origin, "undeployed.txt")          # merged to main, never deployed
    dest = tmp_path / "src"
    _point_at(cloud_runner, monkeypatch, origin, dest, stamp_sha=deployed)

    assert cloud_runner.sync_runner_src() is True
    assert _head(dest) == deployed
    assert not (dest / "undeployed.txt").exists()
    check = cloud_runner._HEALTH["code_clone"]
    assert check["status"] == "ok" and deployed[:12] in check["detail"]


def test_a_rollback_rolls_the_clone_back_too(cloud_runner, monkeypatch, origin, tmp_path):
    older = _commit(origin, "v1.txt")
    newer = _commit(origin, "v2.txt")
    dest = tmp_path / "src"
    _point_at(cloud_runner, monkeypatch, origin, dest, stamp_sha=newer)
    cloud_runner.sync_runner_src()
    assert _head(dest) == newer

    _point_at(cloud_runner, monkeypatch, origin, dest, stamp_sha=older)   # redeployed older
    assert cloud_runner.sync_runner_src() is True
    assert _head(dest) == older and not (dest / "v2.txt").exists()


def test_every_package_the_box_imports_moves_the_deployed_sha(cloud_runner, monkeypatch):
    # The clone is checked out at the deployed sha, so a package outside the sha's
    # path list would neither restart a box when it changes nor roll back with it.
    import pathlib
    import re
    workflow = (pathlib.Path(cloud_runner.__file__).resolve().parents[2]
                / ".github" / "workflows" / "deploy-labs.yml").read_text()
    paths = re.search(r'^\s*PATHS="([^"]+)"', workflow, re.M).group(1).split()
    exposed = []
    monkeypatch.setattr(cloud_runner, "_expose_repo_package",
                        lambda repo, name, purpose, parent="packages": exposed.append(f"{parent}/{name}"))
    cloud_runner._expose_repo_packages(pathlib.Path("/nowhere"))
    assert exposed
    for pkg in exposed:
        assert any(p == pkg or p.startswith(pkg + "/") for p in paths), f"{pkg} not in cloud_sha PATHS"


def test_no_stamp_falls_back_to_main_and_says_so(
        cloud_runner, monkeypatch, origin, tmp_path, capsys):
    tip = _commit(origin, "tip.txt")
    dest = tmp_path / "src"
    _point_at(cloud_runner, monkeypatch, origin, dest)

    assert cloud_runner.sync_runner_src() is True
    assert _head(dest) == tip
    assert cloud_runner._HEALTH["code_clone"]["status"] == "warn"
    assert "names no sha" in capsys.readouterr().out


def test_a_branch_left_in_the_clone_no_longer_disables_the_packages(
        cloud_runner, monkeypatch, origin, tmp_path):
    dest = tmp_path / "src"
    main = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, check=True,
                          capture_output=True, text=True).stdout.strip()
    _point_at(cloud_runner, monkeypatch, origin, dest, stamp_sha=main)
    assert cloud_runner.sync_runner_src() is True
    # Exactly what an agent turn did to /opt/canopy-web: a local branch with a
    # commit origin does not have. `pull --ff-only` refused this.
    _git("checkout", "-qb", "docs/some-spec", cwd=dest)
    (dest / "spec.md").write_text("x")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=dest)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "spec", cwd=dest)

    assert cloud_runner.sync_runner_src() is True
    assert not (dest / "spec.md").exists()  # reset to the deployed sha, not merged
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


@pytest.mark.parametrize("n,status", [(0, "fail"), (1, "ok"), (2, "ok")])
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


def _versions(cloud_runner, monkeypatch, *, cli, clone):
    versions = {"claude": "2.1.266", "canopy": cli}
    monkeypatch.setattr(cloud_runner, "_version_of", lambda cmd: versions.get(pathlib_name(cmd[0]), ""))
    monkeypatch.setattr(cloud_runner, "_marketplace_canopy_version", lambda: clone)


def test_a_box_whose_clone_is_as_stale_as_its_cli_is_behind_the_release(cloud_runner, monkeypatch):
    # CLI and clone both at 0.2.804 must not read "ok" while canopy has released
    # 0.2.805. The server's release is what it is judged by.
    _versions(cloud_runner, monkeypatch, cli="0.2.804", clone="0.2.804")
    cloud_runner._observe_beat({"paused": False, "expected_canopy_version": "0.2.805"})
    monkeypatch.setattr(cloud_runner, "_HEALTH_SLOW_AT", 0.0)
    report = cloud_runner.health_report()
    check = {c["name"]: c for c in report["checks"]}["canopy.cli"]
    assert check["status"] == "warn" and "0.2.805" in check["detail"]
    assert report["canopy_version"] == "0.2.804"


@pytest.mark.parametrize("release", ["0.2.804", "0.2.700", ""])
def test_a_cli_at_or_ahead_of_the_release_or_an_unknown_one_is_ok(cloud_runner, monkeypatch, release):
    _versions(cloud_runner, monkeypatch, cli="0.2.804", clone="0.2.804")
    cloud_runner._observe_beat({"paused": False, "expected_canopy_version": release})
    cloud_runner._slow_checks()
    assert cloud_runner._HEALTH["canopy.cli"]["status"] == "ok"


def test_the_release_is_rejudged_every_beat_not_only_on_the_slow_cadence(cloud_runner, monkeypatch):
    _versions(cloud_runner, monkeypatch, cli="0.2.804", clone="0.2.804")
    cloud_runner._slow_checks()
    assert cloud_runner._HEALTH["canopy.cli"]["status"] == "ok"
    monkeypatch.setattr(cloud_runner, "_HEALTH_SLOW_AT", 10 ** 12)  # no slow pass this beat
    cloud_runner._observe_beat({"expected_canopy_version": "0.2.805"})
    cloud_runner.health_report()
    assert cloud_runner._HEALTH["canopy.cli"]["status"] == "warn"


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
    monkeypatch.setattr(cloud_runner, "IN_FLIGHT_FILE", tmp_path / "in-flight")
    monkeypatch.setattr(cloud_runner, "RUNNER_INSTANCE", "")
    monkeypatch.setattr(cloud_runner, "_stop", False)
    monkeypatch.setattr(cloud_runner, "_EXIT_FOR_REFRESH", False)
    monkeypatch.delenv("CANOPY_RUNNER_SUPERVISED", raising=False)
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


def test_an_unsupervised_box_never_restarts_mid_turn(refresh, monkeypatch):
    cr, calls = refresh
    monkeypatch.setattr(cr, "_in_flight_ids", lambda: ["t1"])
    assert cr.maybe_self_refresh(True, now=2000.0) is False and calls == []


def test_a_supervised_box_refreshes_mid_turn_by_handing_off(refresh, monkeypatch):
    # A refresh that waits for an idle box can sit hours behind chats. A supervisor
    # can carry every turn to the next daemon.
    cr, calls = refresh
    monkeypatch.setattr(cr, "_in_flight_ids", lambda: ["t1", "t2"])
    monkeypatch.setenv("CANOPY_RUNNER_SUPERVISED", "1")
    monkeypatch.setattr(cr, "ADOPT_TURNS", True)
    monkeypatch.setattr(cr, "_adoptable_in_flight", lambda: (2, 0))
    signals = []
    monkeypatch.setattr(cr.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    assert cr.maybe_self_refresh(True, now=2000.0) is True
    assert signals == [(cr.os.getppid(), cr.signal.SIGHUP)] and calls == []
    assert cr._refresh_floor_until() == 2000.0 + cr.REFRESH_MIN_INTERVAL_SECONDS


def test_a_pinned_turn_still_waits(refresh, monkeypatch):
    cr, calls = refresh
    monkeypatch.setattr(cr, "_in_flight_ids", lambda: ["t1"])
    monkeypatch.setenv("CANOPY_RUNNER_SUPERVISED", "1")
    monkeypatch.setattr(cr, "ADOPT_TURNS", True)
    monkeypatch.setattr(cr, "_adoptable_in_flight", lambda: (1, 1))
    monkeypatch.setattr(cr.os, "kill", lambda *a: pytest.fail("signalled"))
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


def test_health_reports_when_the_floor_lets_it_restart_again(refresh):
    # So the runner page can say why a requested refresh waits on an idle box.
    cr, _ = refresh
    assert cr._refresh_floor_until() == 0.0
    assert cr.maybe_self_refresh(True, now=2000.0) is True
    assert cr._refresh_floor_until() == 2000.0 + cr.REFRESH_MIN_INTERVAL_SECONDS
    assert cr.health_report()["refresh_floor_until"] == 2000.0 + cr.REFRESH_MIN_INTERVAL_SECONDS


def test_a_runner_sharing_its_machine_exits_for_its_unit_to_restart_it(refresh, monkeypatch, tmp_path):
    # A `canopy-runner@<id>` runner has no sudo and cannot write /opt/canopy-runner,
    # so its refresh would sit pending, idle, for hours.
    cr, calls = refresh
    monkeypatch.setattr(cr, "RUNNER_INSTANCE", "d0bb2dd6")
    monkeypatch.setattr(cr, "RUNNER_HOME", "/nonexistent/opt/canopy-runner")
    monkeypatch.setattr(cr, "IN_FLIGHT_FILE", tmp_path / "home" / ".canopy-runner" / "in-flight")
    assert cr.maybe_self_refresh(True, now=2000.0) is True
    assert calls == []
    assert cr._stop and cr._EXIT_FOR_REFRESH and cr.REFRESH_EXIT_CODE != 0
    assert cr._refresh_floor_until() == 2000.0 + cr.REFRESH_MIN_INTERVAL_SECONDS


def test_a_refresh_it_cannot_start_says_so_in_the_refresh_check(refresh, monkeypatch, tmp_path):
    cr, _ = refresh
    def denied(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)
    monkeypatch.setattr(cr.subprocess, "run", denied)
    assert cr.maybe_self_refresh(True, now=2000.0) is False
    check = cr._HEALTH["refresh"]
    assert check["status"] == "fail" and "could not restart" in check["detail"]


def test_a_failed_bootstrap_says_why_in_the_check(cloud_runner, monkeypatch, tmp_path, capsys):
    # "exited 1" alone sends someone to journald; the reason is the
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


# ── the vault probe is cached, but not forever ───────────────────────────────

def test_the_vault_probe_is_cached_but_not_forever(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.pathlib.Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(cloud_runner, "_VAULT_PROBE_AT", {})
    monkeypatch.setattr(cloud_runner, "_aws_proof_headers",
                        lambda url: {cloud_runner.AWS_PROOF_HEADER: "p"})
    monkeypatch.setattr(cloud_runner, "_api",
                        lambda m, p, *a, **k: (calls.append(p), (200, {}))[1])
    cloud_runner._agent_env("hal"); cloud_runner._agent_env("hal")
    assert len(calls) == 1
    cloud_runner._VAULT_PROBE_AT["hal"] = 0.0   # expired
    cloud_runner._agent_env("hal")
    assert len(calls) == 2


def test_a_project_turn_carries_no_agent_key(cloud_runner, monkeypatch):
    monkeypatch.setattr(cloud_runner, "_api", lambda *a, **k: pytest.fail("must not resolve"))
    assert cloud_runner._agent_env(None) is not None
