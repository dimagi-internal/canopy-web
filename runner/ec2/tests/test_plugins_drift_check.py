"""A plugin that stopped updating shows on the runner page, not only in a log file.

cloud-ec2-live, 2026-11-06 → 11-18: ace@ace stayed at 0.13.2204 while its clone on the box
(the directory marketplace it is installed from) moved 58 commits to 0.13.2233. Turns load
the plugin CACHE, not the clone, so ACE's Slack turns ran a skill text twelve days old. The
canopy updater that should have moved it failed every session with ENOENT and wrote that only
to ~/.claude/canopy/fleet-update.log. The `plugins` check compares each installed plugin's
`gitCommitSha` with HEAD of the clone it was installed from.
"""
from __future__ import annotations

import json
import subprocess

import pytest


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg, when):
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    (repo / "f.txt").write_text(msg)
    _git(*env_args, "add", "-A", cwd=repo)
    subprocess.run(["git", *env_args, "commit", "-qm", msg], cwd=repo, check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    return _git("rev-parse", "HEAD", cwd=repo)


NOW = 1_795_000_000
DAY = 86400


@pytest.fixture
def box(cloud_runner, monkeypatch, tmp_path):
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"
    plugins.mkdir(parents=True)
    clone = tmp_path / "agents" / "ace"
    clone.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=clone)
    monkeypatch.setattr(cloud_runner, "_HEALTH", {})

    def install(sha, version="0.13.2204", key="ace@ace", loc=None):
        (plugins / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {key: [{"version": version, "gitCommitSha": sha}]}}))
        (plugins / "known_marketplaces.json").write_text(json.dumps(
            {key.split("@")[1]: {"installLocation": str(loc or clone)}}))

    return cloud_runner, home, clone, install


def _check(mod):
    return mod._HEALTH.get("plugins") or {}


def test_a_plugin_days_behind_its_clone_turns_the_check_amber(box):
    mod, home, clone, install = box
    installed = _commit(clone, "0.13.2204", NOW - 13 * DAY)
    _commit(clone, "fix the doc", NOW - 12 * DAY)
    _commit(clone, "0.13.2233", NOW - 1 * DAY)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    c = _check(mod)
    assert c["status"] == "warn"
    assert "ace@ace 0.13.2204 is 2 commits behind" in c["detail"]
    assert "12 days" in c["detail"]
    assert "fleet-update.log" in c["detail"]


def test_a_plugin_at_its_clone_or_just_behind_is_ok(box):
    mod, home, clone, install = box
    installed = _commit(clone, "a", NOW - 3 * DAY)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"
    _commit(clone, "merged an hour ago", NOW - 3600)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"           # the next session's updater takes it


def test_many_commits_behind_warns_even_when_recent(box, monkeypatch):
    mod, home, clone, install = box
    installed = _commit(clone, "base", NOW - 7200)
    for i in range(mod.PLUGIN_STALE_COMMITS + 1):
        _commit(clone, f"c{i}", NOW - 3600)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "warn"


def test_a_sha_the_clone_does_not_know_is_not_judged(box):
    """A marketplace repo that points at a plugin in ANOTHER repo (nova): the installed sha is
    not in the clone's history, so there is nothing honest to compare."""
    mod, home, clone, install = box
    _commit(clone, "marketplace", NOW - 30 * DAY)
    install("0123456789abcdef0123456789abcdef01234567")
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"


def test_unreadable_registry_reports_nothing(box):
    mod, home, _, _ = box
    mod._plugins_check(now=NOW, home=home)
    assert "plugins" not in mod._HEALTH
