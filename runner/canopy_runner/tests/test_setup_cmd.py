"""`canopy-runner setup` — the installer's jobs, rendered into the bundle, never run for real.

No test reaches launchctl, plutil, PowerShell or schtasks: every command goes to a
recording fake.
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess

import pytest

from canopy_runner import platform_jobs, setup_cmd

from .bundle_trees import mac_app, mac_python, windows_install


class Cmds:
    """Records argv; answers per command. `loaded` = labels launchctl print knows."""

    def __init__(self, *, loaded=(), bootstrap_fails=0, ps_rc=0, ps_err=""):
        self.calls: list[list[str]] = []
        self.loaded = set(loaded)
        self.bootstrap_fails = bootstrap_fails
        self.ps_rc, self.ps_err = ps_rc, ps_err

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        rc, err = 0, ""
        if argv[:2] == ["launchctl", "print"]:
            rc = 0 if argv[2].rsplit("/", 1)[1] in self.loaded else 113
        elif argv[:2] == ["launchctl", "bootout"]:
            self.loaded.discard(argv[2].rsplit("/", 1)[1])
        elif argv[:2] == ["launchctl", "bootstrap"]:
            if self.bootstrap_fails:
                self.bootstrap_fails -= 1
                rc, err = 5, "Bootstrap failed: 5: Input/output error"
            else:
                self.loaded.add(os.path.basename(argv[3])[:-len(".plist")])
        elif argv[0] == "powershell.exe":
            rc, err = self.ps_rc, self.ps_err
        return subprocess.CompletedProcess(argv, rc, "", err)

    def named(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


@pytest.fixture
def mac(tmp_path):
    home = tmp_path / "home"
    (home / ".canopy").mkdir(parents=True)
    app = mac_app(home)
    cfg = home / ".canopy" / "runner.json"
    cfg.write_text(json.dumps({"base_url": "https://c.test", "runner_id": "r1", "token": ""}))
    return home, app, cfg


def _setup(home, app, cfg, cmds, **kw):
    out = []
    rc = setup_cmd.run_setup(cfg, home=home, executable=mac_python(app), system="Darwin",
                             run=cmds, sleep=lambda s: None, uid=501, out=out.append,
                             env=kw.pop("env", {}), **kw)
    return rc, out


def test_refuses_off_the_bundle(tmp_path):
    out = []
    assert setup_cmd.run_setup(tmp_path / "runner.json", out=out.append) == 2
    assert "install-runner.sh" in out[0]


def test_linux_is_not_supported(tmp_path):
    app = mac_app(tmp_path)
    out = []
    rc = setup_cmd.run_setup(tmp_path / "r.json", executable=mac_python(app), system="Linux",
                             out=out.append)
    assert rc == 1 and "macOS and Windows only" in out[0]


def test_macos_renders_three_jobs_into_the_app_and_starts_them(mac, monkeypatch):
    home, app, cfg = mac
    monkeypatch.setattr(setup_cmd.shutil, "which", lambda n: "/usr/bin/plutil")
    cmds = Cmds()
    rc, out = _setup(home, app, cfg, cmds)
    assert rc == 0, out
    rt = app / "Contents" / "Resources" / "runtime"
    agents = home / "Library" / "LaunchAgents"
    runner = plistlib.loads((agents / "com.canopy.runner.plist").read_bytes())
    assert runner["ProgramArguments"] == [str(rt / "bin" / "canopy-runner"), "run",
                                          "--config", str(cfg)]
    assert runner["KeepAlive"] is True and runner["RunAtLoad"] is True
    assert runner["StandardOutPath"] == str(home / ".canopy" / "runner.log")
    assert runner["AssociatedBundleIdentifiers"] == ["com.canopy.runner.menubar"]
    path = runner["EnvironmentVariables"]["PATH"].split(":")
    assert path[0] == str(rt / "node" / "bin") and "/opt/homebrew/bin" in path
    updater = plistlib.loads((agents / "com.canopy.runner.updater.plist").read_bytes())
    assert updater["ProgramArguments"][1:4] == ["self-update", "--if-stale", "--config"]
    assert updater["StartInterval"] == 1800 and updater["RunAtLoad"] is False
    assert updater["StandardOutPath"].endswith("updater.log")
    menubar = plistlib.loads((agents / "com.canopy.runner.menubar.plist").read_bytes())
    assert menubar["ProgramArguments"] == ["/usr/bin/open", "-g", str(app)]
    # Nothing versioned in any path: they must survive an update untouched.
    for p in (runner, updater, menubar):
        assert "a" * 12 not in json.dumps(p)
    assert len(cmds.named("/usr/bin/plutil", "-lint")) == 3
    booted = [os.path.basename(c[3]) for c in cmds.named("launchctl", "bootstrap")]
    assert booted == ["com.canopy.runner.plist", "com.canopy.runner.updater.plist",
                      "com.canopy.runner.menubar.plist"]
    link = home / ".canopy" / "bin" / "canopy-device-key"
    assert link.is_symlink() and os.readlink(link) == str(rt / "bin" / "canopy-device-key")


def test_a_second_run_restarts_only_the_runner(mac):
    home, app, cfg = mac
    _setup(home, app, cfg, Cmds())
    cmds = Cmds(loaded={"com.canopy.runner", "com.canopy.runner.updater",
                        "com.canopy.runner.menubar"})
    rc, _ = _setup(home, app, cfg, cmds)
    assert rc == 0
    assert cmds.named("launchctl", "bootout") == [] and cmds.named("launchctl", "bootstrap") == []
    assert cmds.named("launchctl", "kickstart") == [
        ["launchctl", "kickstart", "-k", "gui/501/com.canopy.runner"]]


def test_never_bounces_the_updater_it_runs_under(mac):
    home, app, cfg = mac
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.canopy.runner.updater.plist").write_bytes(b"old definition")
    cmds = Cmds(loaded={"com.canopy.runner.updater"})
    rc, out = _setup(home, app, cfg, cmds, env={"XPC_SERVICE_NAME": "com.canopy.runner.updater"})
    assert rc == 0
    touched = " ".join(" ".join(c) for c in cmds.calls if c[0] == "launchctl")
    assert "com.canopy.runner.updater" not in touched
    assert b"self-update" in (agents / "com.canopy.runner.updater.plist").read_bytes()
    assert any("loads next login" in line for line in out)


def test_a_bootstrap_that_keeps_failing_is_loud(mac):
    home, app, cfg = mac
    cmds = Cmds(bootstrap_fails=99)
    rc, out = _setup(home, app, cfg, cmds)
    assert rc == 1 and "would not start com.canopy.runner" in out[-1]
    assert len(cmds.named("launchctl", "bootstrap")) == 5


def test_a_race_on_bootstrap_is_retried(mac):
    home, app, cfg = mac
    cmds = Cmds(bootstrap_fails=2)
    assert _setup(home, app, cfg, cmds)[0] == 0


def test_pairs_first_when_there_is_no_config(mac):
    home, app, cfg = mac
    cfg.unlink()
    seen = {}

    def fake_pair(path, **kw):
        seen.update(kw, path=path)
        path.write_text(json.dumps({"base_url": "https://c.test", "runner_id": "r9",
                                    "token": ""}))
        return 0

    rc, _ = _setup(home, app, cfg, Cmds(), pair_fn=fake_pair, base_url="https://x.test")
    assert rc == 0 and seen["path"] == cfg and seen["base_url"] == "https://x.test"
    # The helper is linked BEFORE pairing, so the enclave key is made where canopy looks.
    assert (home / ".canopy" / "bin" / "canopy-device-key").is_symlink()


def test_no_pair_without_a_config_installs_nothing(mac):
    home, app, cfg = mac
    cfg.unlink()
    cmds = Cmds()
    rc, out = _setup(home, app, cfg, cmds, no_pair=True)
    assert rc == 1 and not cmds.named("launchctl")


def test_a_failed_pairing_installs_nothing(mac):
    from canopy_runner.pair import PairError

    home, app, cfg = mac
    cfg.unlink()

    def declined(path, **kw):
        raise PairError("the pairing was declined")

    cmds = Cmds()
    rc, out = _setup(home, app, cfg, cmds, pair_fn=declined)
    assert rc == 1 and "declined" in out[-1] and not cmds.named("launchctl")


# --- Windows ---------------------------------------------------------------------


@pytest.fixture
def win(tmp_path):
    home = tmp_path / "home"
    (home / ".canopy").mkdir(parents=True)
    app = windows_install(tmp_path / "Programs")
    cfg = home / ".canopy" / "runner.json"
    cfg.write_text("{}")
    return home, app, cfg


def _win_setup(home, app, cfg, cmds, env=None):
    out = []
    rc = setup_cmd.run_setup(cfg, home=home, executable=str(app / "current" / "python" /
                                                             "pythonw.exe"),
                             system="Windows", run=cmds, out=out.append, env=env or {})
    return rc, out


def test_windows_registers_both_tasks_through_current(win):
    home, app, cfg = win
    cmds = Cmds()
    rc, out = _win_setup(home, app, cfg, cmds)
    assert rc == 0, out
    scripts = [c[-1] for c in cmds.named("powershell.exe")]
    assert len(scripts) == 2
    runner, updater = scripts
    pyw = str(app / "current" / "python" / "pythonw.exe")
    assert f"-Execute '{pyw}'" in runner and "versions" not in runner
    assert "canopy_runner.main run --config" in runner and "--log-file" in runner
    assert "runner.log" in runner
    assert "New-ScheduledTaskTrigger -AtLogOn -User $u" in runner
    assert "WindowsIdentity]::GetCurrent().Name" in runner
    assert "-LogonType Interactive -RunLevel Limited" in runner
    assert "-RestartCount 999" in runner and "-ExecutionTimeLimit (New-TimeSpan -Seconds 0)" in runner
    assert "-TaskName 'canopy-runner' -TaskPath '\\Canopy'" in runner
    assert "self-update --if-stale" in updater and "updater.log" in updater
    assert "-RepetitionInterval (New-TimeSpan -Minutes 30)" in updater
    assert cmds.named("schtasks") == [["schtasks", "/Run", "/TN", r"\Canopy\canopy-runner"]]
    for c in cmds.named("powershell.exe"):
        assert c[1:5] == ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]


def test_windows_does_not_re_register_the_updater_it_runs_under(win):
    home, app, cfg = win
    _win_setup(home, app, cfg, Cmds())
    cmds = Cmds()
    rc, out = _win_setup(home, app, cfg, cmds, env={"CANOPY_RUNNER_UNDER_JOB": "updater"})
    assert rc == 0
    assert len(cmds.named("powershell.exe")) == 1, "only the runner task"


def test_windows_access_denied_says_what_to_do(win):
    home, app, cfg = win
    rc, out = _win_setup(home, app, cfg, Cmds(ps_rc=1, ps_err="Access is denied."))
    assert rc == 1 and "non-administrator" in out[-1]


def test_quoting_survives_an_apostrophe_in_the_profile_path():
    script = setup_cmd.register_script("canopy-runner", r"C:\Users\O'Brien\p.exe", "-m x",
                                       updater=False)
    assert r"-Execute 'C:\Users\O''Brien\p.exe'" in script


def test_kickstart_updater_names_the_bundled_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: True)
    platform_jobs.kickstart_updater(runner=lambda argv, **kw: calls.append(argv))
    assert calls == [["schtasks", "/Run", "/TN", r"\Canopy\canopy-runner-updater"]]
