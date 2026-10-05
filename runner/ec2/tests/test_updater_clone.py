"""A fresh cloud box gets the clone its auto-updater reads from.

cloud-ec2-2 (built 2026-10-05) logged "no update_runner.sh in /opt/canopy-web yet;
skipping" on every updater tick: cloud-init only creates the directory, and
nothing cloned into it once the runner moved to RUNNER_SRC_DIR. The box never
updated. cloud-ec2-1 only did because its clone predated that move.
"""
from __future__ import annotations

import subprocess


def _origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "update_runner.sh").write_text("echo hi\n")
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1"],
                   cwd=origin, check=True)
    return origin


def _setup(cloud_runner, monkeypatch, tmp_path, repo):
    monkeypatch.setattr(cloud_runner, "CANOPY_WEB_REPO_DIR", str(repo))
    monkeypatch.setattr(cloud_runner, "CANOPY_WEB_REPO_URL", str(_origin(tmp_path)))
    checks = {}
    monkeypatch.setattr(cloud_runner, "_set_check", lambda n, s, d="": checks.__setitem__(n, (s, d)))
    return checks


def test_the_empty_directory_cloud_init_makes_becomes_a_clone(cloud_runner, monkeypatch, tmp_path):
    repo = tmp_path / "opt-canopy-web"
    repo.mkdir()  # what cloud-init leaves
    checks = _setup(cloud_runner, monkeypatch, tmp_path, repo)
    assert cloud_runner.ensure_updater_clone() is True
    shown = subprocess.run(["git", "-C", str(repo), "show", "origin/main:update_runner.sh"],
                           capture_output=True, text=True)
    assert shown.stdout == "echo hi\n"  # exactly what the updater shim reads
    assert checks["updater_clone"][0] == "ok"


def test_an_existing_clone_is_left_alone(cloud_runner, monkeypatch, tmp_path):
    repo = tmp_path / "opt-canopy-web"
    (repo / ".git").mkdir(parents=True)
    checks = _setup(cloud_runner, monkeypatch, tmp_path, repo)
    assert cloud_runner.ensure_updater_clone() is True
    assert checks["updater_clone"][0] == "ok"


def test_a_directory_that_cannot_be_cloned_into_says_the_box_will_not_update(cloud_runner, monkeypatch, tmp_path):
    repo = tmp_path / "opt-canopy-web"
    repo.mkdir()
    (repo / "stray").write_text("x")
    checks = _setup(cloud_runner, monkeypatch, tmp_path, repo)
    assert cloud_runner.ensure_updater_clone() is False
    assert checks["updater_clone"][0] == "fail"


def test_bootstrap_ensures_it_first(cloud_runner, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud_runner, "ensure_updater_clone", lambda: calls.append("clone") or True)
    monkeypatch.setattr(cloud_runner, "sync_runner_src", lambda: calls.append("src") or False)
    monkeypatch.setattr(cloud_runner, "_set_check", lambda *a, **k: None)
    cloud_runner._run_bootstrap()
    assert calls == ["clone", "src"]
