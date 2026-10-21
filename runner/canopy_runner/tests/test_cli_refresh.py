"""The canopy CLI follows the plugin: reinstalled when behind, left alone otherwise."""
import json
import subprocess

import pytest

from canopy_runner import cli_refresh


@pytest.fixture
def box(tmp_path, monkeypatch):
    manifest = tmp_path / ".claude/plugins/marketplaces/canopy/plugins/canopy/.claude-plugin"
    manifest.mkdir(parents=True)
    (manifest / "plugin.json").write_text(json.dumps({"version": "0.2.750"}))
    bin_ = tmp_path / ".local/bin"
    bin_.mkdir(parents=True)
    for name in ("canopy", "uv"):
        (bin_ / name).write_text("")
        (bin_ / name).chmod(0o755)
    monkeypatch.setattr(cli_refresh.shutil, "which", lambda _n: None)
    monkeypatch.setattr(cli_refresh, "canopy_cli", lambda home: str(bin_ / "canopy"))
    from canopy_runner import desktop
    monkeypatch.setattr(desktop, "claude_cli", lambda: None)
    return tmp_path


def _runner(version, calls):
    def run(cmd, **kw):
        calls.append(cmd)
        out = f"canopy {version}\n" if cmd[-1] == "--version" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return run


def test_a_cli_behind_the_plugin_is_reinstalled_from_the_clone(box):
    calls = []
    assert cli_refresh.check(home=box, run=_runner("0.2.685", calls)) == "refreshed"
    install = calls[-1]
    assert install[1:5] == ["tool", "install", "--reinstall", "--force"]
    assert install[-1] == str(box / ".claude/plugins/marketplaces/canopy")


@pytest.mark.parametrize("version", ["0.2.750", "0.2.760"])
def test_a_current_or_newer_cli_is_left_alone(box, version):
    calls = []
    assert cli_refresh.check(home=box, run=_runner(version, calls)) == "current"
    assert not any("install" in c for c in calls)


def test_no_clone_or_no_cli_does_nothing(box, monkeypatch):
    calls = []
    monkeypatch.setattr(cli_refresh, "canopy_cli", lambda home: None)
    assert cli_refresh.check(home=box, run=_runner("0.2.1", calls)) == "no canopy CLI"
    assert cli_refresh.check(home=box / "nowhere", run=_runner("0.2.1", calls)) == \
        "no canopy marketplace clone"
    assert not any("install" in c for c in calls)


def test_a_failed_reinstall_is_logged_and_retried_later(box):
    def run(cmd, **kw):
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "canopy 0.2.685", "")
        return subprocess.CompletedProcess(cmd, 1, "", "canopy.exe is in use")
    assert cli_refresh.check(home=box, run=run) == "reinstall failed"


def test_start_checks_on_a_cadence_and_can_be_turned_off(box, monkeypatch):
    seen = []
    monkeypatch.setattr(cli_refresh, "check", lambda **kw: seen.append(kw))
    cli_refresh.start(every=0, sleep=lambda _s: None, max_checks=2, home=box).join(5)
    assert len(seen) == 2
    monkeypatch.setenv("CANOPY_CLI_REFRESH", "0")
    assert cli_refresh.start() is None
