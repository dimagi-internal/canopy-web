"""A turn runs the agent's plugin as pulled at claim, and a plugin that did not install
shows on the runner page, not only in a log file.

Turns load the plugin CACHE, not the clone. A claim pulls the agent's clone and installs it
with canopy's updater (`--refresh`); the `plugins` check goes amber when an installed plugin
still lags its clone PLUGIN_REFRESH_GRACE_SECONDS after the clone last moved.
"""
from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

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


def _moved(clone, when):
    os.utime(clone / ".git" / "logs" / "HEAD", (when, when))


def test_a_plugin_behind_a_clone_that_moved_long_ago_turns_the_check_amber(box):
    mod, home, clone, install = box
    installed = _commit(clone, "0.13.2204", NOW - 13 * DAY)
    _commit(clone, "fix the doc", NOW - 12 * DAY)
    _commit(clone, "0.13.2233", NOW - 1 * DAY)
    _moved(clone, NOW - 12 * DAY)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    c = _check(mod)
    assert c["status"] == "warn"
    assert "ace@ace 0.13.2204 is 2 commits behind" in c["detail"]
    assert "moved 12 days ago" in c["detail"]
    assert "fleet-update.log" in c["detail"]


def test_one_old_commit_not_installed_past_the_grace_is_amber(box):
    """The commit's own date does not matter: a claim installs what it pulls, at once."""
    mod, home, clone, install = box
    installed = _commit(clone, "a", NOW - 3 * DAY)
    _commit(clone, "b", NOW - 3 * DAY)
    _moved(clone, NOW - mod.PLUGIN_REFRESH_GRACE_SECONDS - 60)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "warn"
    assert "1 commit behind" in _check(mod)["detail"]


def test_a_plugin_at_its_clone_or_inside_the_grace_is_ok(box):
    mod, home, clone, install = box
    installed = _commit(clone, "a", NOW - 3 * DAY)
    install(installed)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"
    _commit(clone, "pulled a minute ago", NOW - 30 * DAY)
    _moved(clone, NOW - 60)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"           # the claim that pulled it is installing it


def test_a_sha_the_clone_does_not_know_is_not_judged(box):
    """A marketplace repo that points at a plugin in ANOTHER repo (nova): the installed sha is
    not in the clone's history, so there is nothing honest to compare."""
    mod, home, clone, install = box
    _commit(clone, "marketplace", NOW - 30 * DAY)
    _moved(clone, NOW - 30 * DAY)
    install("0123456789abcdef0123456789abcdef01234567")
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"


def test_unreadable_registry_reports_nothing(box):
    mod, home, _, _ = box
    mod._plugins_check(now=NOW, home=home)
    assert "plugins" not in mod._HEALTH


# ── what is installed, named even when all is well ──────────────────────────


def _install_many(home, entries, markets):
    plugins = home / ".claude" / "plugins"
    (plugins / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {k: [v] for k, v in entries.items()}}))
    (plugins / "known_marketplaces.json").write_text(json.dumps(
        {m: {"installLocation": str(loc)} for m, loc in markets.items()}))


def test_an_ok_check_names_each_installed_version(box, monkeypatch):
    mod, home, clone, install = box
    monkeypatch.setitem(mod._CANOPY_RELEASE, "v", "")
    sha = _commit(clone, "a", NOW - 3 * DAY)
    install(sha, version="0.13.2204")
    mod._plugins_check(now=NOW, home=home)
    c = _check(mod)
    assert c["status"] == "ok"
    assert c["detail"] == "ace 0.13.2204 (each current with its clone)"


def test_plugins_it_could_not_compare_are_named_with_why(box, tmp_path, monkeypatch):
    mod, home, clone, _ = box
    monkeypatch.setitem(mod._CANOPY_RELEASE, "v", "")
    sha = _commit(clone, "a", NOW - 3 * DAY)
    _install_many(home, {
        "canopy@canopy": {"version": "0.2.920", "gitCommitSha": sha},
        "ace@ace": {"version": "0.13.2204", "gitCommitSha": sha},
        "nova@nova": {"version": "1.2.0", "gitCommitSha": "f" * 40},
        "bare@bare": {"version": "0.1.0"},
    }, {"canopy": clone, "ace": clone, "nova": tmp_path / "nowhere"})
    mod._plugins_check(now=NOW, home=home)
    c = _check(mod)
    assert c["status"] == "ok"
    assert c["detail"].startswith("canopy 0.2.920 · ace 0.13.2204 · bare 0.1.0 · nova 1.2.0 (")
    assert "the rest current with their clones" in c["detail"]
    assert "nova not compared: no clone" in c["detail"]
    assert "bare not compared: no installed sha" in c["detail"]


def test_a_plugin_installed_ahead_of_its_clone_is_named_not_compared(box):
    mod, home, clone, install = box
    _commit(clone, "marketplace", NOW - 30 * DAY)
    install("0123456789abcdef0123456789abcdef01234567")
    mod._plugins_check(now=NOW, home=home)
    assert "ace not compared: its sha is not in its clone's history" in _check(mod)["detail"]


def test_the_list_of_what_is_installed_is_bounded(box, tmp_path):
    mod, home, _, _ = box
    many = {f"plugin-{i:03}@m{i}": {"version": f"{i}.0.0"} for i in range(200)}
    _install_many(home, many, {})
    mod._plugins_check(now=NOW, home=home)
    d = _check(mod)["detail"]
    assert len(d) <= mod.PLUGINS_SUMMARY_CHARS
    assert "more" in d


def test_a_canopy_plugin_older_than_canopys_release_is_amber(box, monkeypatch):
    """Its clone may be just as old, so the clone alone reads consistent: canopy's release
    (the heartbeat's expected_canopy_version) is the honest yardstick, as for the CLI."""
    mod, home, clone, _ = box
    sha = _commit(clone, "a", NOW - 3 * DAY)
    _install_many(home, {"canopy@canopy": {"version": "0.2.900", "gitCommitSha": sha}},
                  {"canopy": clone})
    monkeypatch.setitem(mod._CANOPY_RELEASE, "v", "0.2.920")
    mod._plugins_check(now=NOW, home=home)
    c = _check(mod)
    assert c["status"] == "warn"
    assert c["detail"].startswith(
        "canopy plugin 0.2.900, but canopy's release is 0.2.920: this runner refreshes when idle")
    assert "Installed: canopy 0.2.900" in c["detail"]


@pytest.mark.parametrize("release, version", [("", "0.2.900"), ("0.2.920", "0.2.920"),
                                              ("0.2.920", "0.2.921"), ("0.2.920", "")])
def test_canopy_at_or_past_its_release_or_unknown_is_never_stale(box, monkeypatch, release, version):
    mod, home, clone, _ = box
    sha = _commit(clone, "a", NOW - 3 * DAY)
    _install_many(home, {"canopy@canopy": {"version": version, "gitCommitSha": sha}},
                  {"canopy": clone})
    monkeypatch.setitem(mod._CANOPY_RELEASE, "v", release)
    mod._plugins_check(now=NOW, home=home)
    assert _check(mod)["status"] == "ok"


# ── which plugins a turn loaded ─────────────────────────────────────────────


def test_a_turn_notes_the_installed_plugins_and_its_finish_carries_them(box):
    mod, home, _, _ = box
    _install_many(home, {
        "canopy@canopy": {"version": "0.2.920", "gitCommitSha": "a" * 40, "installPath": "/x"},
        "ace@ace": {"version": "0.13.2204", "gitCommitSha": "b" * 40},
    }, {})
    mod._note_loaded_plugins("t1", home=home)
    _install_many(home, {"canopy@canopy": {"version": "9.9.9"}}, {})
    mod._note_loaded_plugins("t1", home=home)          # the first note wins
    assert mod._loaded_plugins_take("t1") == {
        "ace@ace": {"version": "0.13.2204", "sha": "b" * 40},
        "canopy@canopy": {"version": "0.2.920", "sha": "a" * 40},
    }
    assert mod._loaded_plugins_take("t1") is None


def test_a_lean_turn_notes_only_the_plugins_it_keeps(box):
    mod, home, _, _ = box
    _install_many(home, {"canopy@canopy": {"version": "0.2.920"}, "ace@ace": {"version": "1"}}, {})
    mod._note_loaded_plugins("t2", lean=True, home=home)
    assert list(mod._loaded_plugins_take("t2")) == ["canopy@canopy"]


def test_an_unreadable_registry_notes_nothing_to_send(box):
    mod, home, _, _ = box
    mod._note_loaded_plugins("t3", home=home)
    assert mod._loaded_plugins_take("t3") is None


# ── the claim installs what it pulled ───────────────────────────────────────


@pytest.fixture
def updater(cloud_runner, monkeypatch, tmp_path):
    script = tmp_path / "canopy" / "hooks" / "fleet_session_start_update.py"
    script.parent.mkdir(parents=True)
    script.write_text("def refresh_plugin(): ...\n")
    monkeypatch.setattr(cloud_runner, "_canopy_fleet_updater", lambda: script)
    calls, logs = [], []
    monkeypatch.setattr(cloud_runner, "_log", logs.append)

    def answer(rc, out):
        def run(argv, **kw):
            calls.append((argv, kw.get("timeout")))
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        monkeypatch.setattr(cloud_runner.subprocess, "run", run)

    return cloud_runner, script, calls, logs, answer


def test_a_claim_installs_the_pulled_clone_with_canopys_updater(updater, tmp_path):
    mod, script, calls, logs, answer = updater
    answer(0, "ace 0.13.2204→0.13.2234 (sha 3012a80e→41502e5a)\n")
    mod._refresh_agent_plugin("ace", tmp_path / "agents" / "ace")
    assert calls == [(["python3", str(script), "--refresh", "ace", "--clone",
                       str(tmp_path / "agents" / "ace")], mod.PLUGIN_REFRESH_TIMEOUT)]
    assert logs == ["plugin: ace 0.13.2204→0.13.2234 (sha 3012a80e→41502e5a)"]


def test_an_installed_clone_logs_nothing(updater, tmp_path):
    mod, _script, calls, logs, answer = updater
    answer(0, "ace up-to-date\n")
    mod._refresh_agent_plugin("ace", tmp_path / "agents" / "ace")
    assert len(calls) == 1 and logs == []


def test_a_failed_install_is_logged_and_the_turn_still_runs(updater, tmp_path):
    mod, _script, _calls, logs, answer = updater
    answer(1, "ace error: sync: copy failed\n")
    mod._refresh_agent_plugin("ace", tmp_path / "agents" / "ace")
    assert len(logs) == 1 and "ace error: sync: copy failed" in logs[0]
    assert "runs the version already installed" in logs[0]


def test_no_canopy_updater_on_the_box_means_nothing_is_run(cloud_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(cloud_runner.subprocess, "run",
                        lambda *a, **k: pytest.fail("nothing to run"))
    cloud_runner._refresh_agent_plugin("ace", tmp_path / "agents" / "ace")


def test_an_updater_without_refresh_is_never_used(load_cloud_runner, monkeypatch, tmp_path):
    """An older canopy updater ignores `--refresh` and runs a whole-fleet update: never at claim."""
    mod = load_cloud_runner()
    root = tmp_path / "canopy"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "fleet_session_start_update.py").write_text("def main(): ...\n")
    monkeypatch.setattr(mod, "_canopy_plugin_root", lambda: root)
    import importlib.util
    spec = importlib.util.spec_from_file_location("cr_real", mod.__file__)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "_canopy_plugin_root", lambda: root)
    assert real._canopy_fleet_updater() is None
    (root / "hooks" / "fleet_session_start_update.py").write_text("def refresh_plugin(): ...\n")
    assert real._canopy_fleet_updater() == root / "hooks" / "fleet_session_start_update.py"
