"""`canopy-runner self-update` — the bundled updater, with every download and command faked."""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import self_update, update
from canopy_runner.client import ClientError

from .bundle_trees import add_windows_version, mac_app, mac_python, windows_install

OLD, NEW = "a" * 40, "b" * 40


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.paths = []

    def _call(self, method, path, body=None, **kw):
        self.paths.append((method, path))
        if isinstance(self.answer, Exception):
            raise self.answer
        return 200, self.answer


def _rel(sha=NEW, data=b"payload", file="Canopy-Runner-macos-arm64.app.zip"):
    return {"sha": sha, "platform": "macos-arm64", "file": file, "url": "https://s3.test/x",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "signed": True, "notarized": True}


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(state_path=str(tmp_path / "state" / "runner-state.json"),
                           runner_id="r1")


# --- the decision ------------------------------------------------------------------


def test_asks_the_release_route_for_this_platform(cfg):
    client = FakeClient(_rel())
    assert self_update.decide(cfg, client, installed=OLD, platform="macos-arm64")[0] == "stale"
    assert client.paths == [("GET", "/runner-releases/current?platform=macos-arm64")]


@pytest.mark.parametrize("answer,installed,want", [
    (ClientError("GET -> 404", status=404), OLD, "unknown"),
    (ClientError("GET -> 503", status=503, transient=True), OLD, "unknown"),
    ({"sha": ""}, OLD, "unknown"),
    ("not a dict", OLD, "unknown"),
    (_rel(), "", "unknown"),
    (_rel(sha=OLD), OLD, "current"),
])
def test_the_vocabulary(cfg, answer, installed, want):
    assert self_update.decide(cfg, FakeClient(answer), installed=installed,
                              platform="macos-arm64")[0] == want


def test_never_while_a_turn_is_in_flight(cfg):
    update.mark_busy(cfg, 1)
    assert self_update.decide(cfg, FakeClient(_rel()), installed=OLD,
                              platform="macos-arm64")[0] == "busy"


# --- download ----------------------------------------------------------------------


def _opener(data):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda url, timeout=None: Resp(data)


def test_download_checks_the_sha256(tmp_path):
    rel = _rel(data=b"payload")
    got = self_update.download(rel, tmp_path / "u", opener=_opener(b"payload"))
    assert got.read_bytes() == b"payload"
    bad = _rel(data=b"payload")
    bad["sha256"] = hashlib.sha256(b"other").hexdigest()
    with pytest.raises(self_update.UpdateError, match="sha256"):
        self_update.download(bad, tmp_path / "v", opener=_opener(b"payload"))
    assert list((tmp_path / "v").iterdir()) == [], "nothing half-written is left"


def test_download_checks_the_size(tmp_path):
    with pytest.raises(self_update.UpdateError, match="larger"):
        self_update.download(_rel(data=b"abc"), tmp_path, opener=_opener(b"abcdef"))
    rel = _rel(data=b"abc")
    rel["size"] = 0
    with pytest.raises(self_update.UpdateError, match="believable"):
        self_update.download(rel, tmp_path, opener=_opener(b"abc"))


# --- macOS swap ------------------------------------------------------------------


class MacCmds:
    """ditto unpacks a new app; codesign answers per app; everything else succeeds."""

    def __init__(self, *, new_sha=NEW, verify_rc=0, teams=None):
        self.new_sha = new_sha
        self.verify_rc = verify_rc
        self.teams = teams or {}
        self.calls = []

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append((argv, kw.get("env")))
        if argv[0] == "ditto":
            staging = Path(argv[-1])
            mac_app(staging / "x", self.new_sha)  # builds x/Applications/Canopy Runner.app
            os.rename(staging / "x" / "Applications" / "Canopy Runner.app",
                      staging / "Canopy Runner.app")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["codesign", "--verify"]:
            return subprocess.CompletedProcess(argv, self.verify_rc, "", "invalid signature")
        if argv[:2] == ["codesign", "-dv"]:
            kind = "new" if ".new-" in argv[-1] else "running"
            team = self.teams.get(kind, "")
            return subprocess.CompletedProcess(argv, 0, "",
                                               f"Identifier=x\nTeamIdentifier={team or 'not set'}\n")
        return subprocess.CompletedProcess(argv, 0, "setup ok\n", "")

    def ran(self, name):
        return [a for a, _ in self.calls if a[0].endswith(name)]


def _sha_of(app):
    return json.loads((app / "Contents/Resources/runtime/release.json").read_text())["sha"]


def test_mac_swap_installs_runs_the_new_setup_and_relaunches_the_menu_bar(tmp_path):
    app = mac_app(tmp_path)
    cmds = MacCmds(teams={"running": "TEAM1", "new": "TEAM1"})
    self_update.install_macos(tmp_path / "u.zip", NEW, app, tmp_path / "runner.json",
                              run=cmds, out=lambda *a: None)
    assert _sha_of(app) == NEW
    assert sorted(p.name for p in app.parent.iterdir()) == ["Canopy Runner.app"]
    (setup_argv, env), = [(a, e) for a, e in cmds.calls if a[0].endswith("bin/canopy-runner")]
    assert setup_argv[1:] == ["setup", "--no-pair", "--config", str(tmp_path / "runner.json")]
    assert env["CANOPY_RUNNER_UNDER_JOB"] == "updater"
    assert cmds.ran("pkill") == [["pkill", "-f", str(app / "Contents/MacOS/CanopyRunner")]]
    assert cmds.ran("open") == [["open", "-g", str(app)]]


@pytest.mark.parametrize("cmds,match", [
    (MacCmds(verify_rc=1), "does not verify"),
    (MacCmds(teams={"running": "TEAM1", "new": "EVIL"}), "signed by team EVIL"),
    (MacCmds(teams={"running": "TEAM1"}), "signed by team"),
    (MacCmds(new_sha="c" * 40), "release.json"),
])
def test_mac_refusals_leave_the_running_app_untouched(tmp_path, cmds, match):
    app = mac_app(tmp_path)
    with pytest.raises(self_update.UpdateError, match=match):
        self_update.install_macos(tmp_path / "u.zip", NEW, app, tmp_path / "r.json",
                                  run=cmds, out=lambda *a: None)
    assert _sha_of(app) == OLD
    assert sorted(p.name for p in app.parent.iterdir()) == ["Canopy Runner.app"]
    assert not cmds.ran("bin/canopy-runner"), "no setup for a refused update"


def test_an_unsigned_running_build_accepts_on_its_sha256(tmp_path):
    app = mac_app(tmp_path)
    self_update.install_macos(tmp_path / "u.zip", NEW, app, tmp_path / "r.json",
                              run=MacCmds(teams={"new": "TEAM1"}), out=lambda *a: None)
    assert _sha_of(app) == NEW


def test_a_failed_second_rename_rolls_back(tmp_path, monkeypatch):
    app = mac_app(tmp_path)
    real = os.rename
    n = {"i": 0}

    def flaky(src, dst):
        if Path(dst) == app and ".new-" in str(src):
            raise OSError("disk full")
        n["i"] += 1
        return real(src, dst)

    monkeypatch.setattr(self_update.os, "rename", flaky)
    with pytest.raises(OSError):
        self_update.install_macos(tmp_path / "u.zip", NEW, app, tmp_path / "r.json",
                                  run=MacCmds(), out=lambda *a: None)
    assert _sha_of(app) == OLD, "the old app is back where it was"


# --- the command -------------------------------------------------------------------


def _config(tmp_path):
    p = tmp_path / "runner.json"
    p.write_text(json.dumps({"base_url": "https://c.test", "runner_id": "r1", "token": "",
                             "state_path": str(tmp_path / "runner-state.json")}))
    return p


def test_not_bundled_is_a_quiet_no(tmp_path):
    out = []
    assert self_update.run_self_update(_config(tmp_path), out=out.append) == 0
    assert "install-runner.sh" in out[-1]


def test_stale_with_if_stale_installs_the_servers_sha(tmp_path):
    app = mac_app(tmp_path / "home")
    seen = {}

    def downloader(rel, dest):
        seen["rel"], seen["dest"] = rel, dest
        dest.mkdir(parents=True)
        (dest / rel["file"]).write_bytes(b"zip")
        return dest / rel["file"]

    out = []
    rc = self_update.run_self_update(
        _config(tmp_path), if_stale=True, executable=mac_python(app), home=tmp_path / "home",
        client_factory=lambda cfg: FakeClient(_rel()), installed=OLD, downloader=downloader,
        run=MacCmds(), system="Darwin", out=out.append)
    assert rc == 0 and out[-1] == f"stale {NEW}"
    assert seen["rel"]["sha"] == NEW and seen["dest"] == tmp_path / "home/.canopy/updates" / NEW
    assert _sha_of(app) == NEW
    assert not (tmp_path / "home/.canopy/updates" / NEW).exists(), "the download is cleaned up"


def test_without_if_stale_nothing_is_installed(tmp_path):
    app = mac_app(tmp_path / "home")
    out = []
    rc = self_update.run_self_update(
        _config(tmp_path), executable=mac_python(app), home=tmp_path / "home",
        client_factory=lambda cfg: FakeClient(_rel()), installed=OLD,
        downloader=lambda *a: pytest.fail("no download"), system="Darwin", out=out.append)
    assert rc == 0 and out == [f"stale {NEW}"]


def test_busy_and_unknown_never_download(tmp_path):
    app = mac_app(tmp_path / "home")
    cfgp = _config(tmp_path)
    update.mark_busy(SimpleNamespace(state_path=str(tmp_path / "runner-state.json")), 2)
    out = []
    kw = dict(if_stale=True, executable=mac_python(app), home=tmp_path / "home",
              installed=OLD, downloader=lambda *a: pytest.fail("no download"),
              system="Darwin", out=out.append)
    assert self_update.run_self_update(
        cfgp, client_factory=lambda c: FakeClient(_rel()), **kw) == 0
    assert out[-1] == f"busy {NEW}"
    assert self_update.run_self_update(
        cfgp, client_factory=lambda c: FakeClient(ClientError("x", status=404)), **kw) == 0
    assert out[-1] == "unknown -"


def test_a_failed_install_reports_and_exits_nonzero(tmp_path):
    app = mac_app(tmp_path / "home")

    def bad_download(rel, dest):
        raise self_update.UpdateError("the download's sha256 does not match")

    out = []
    rc = self_update.run_self_update(
        _config(tmp_path), if_stale=True, executable=mac_python(app), home=tmp_path / "home",
        client_factory=lambda c: FakeClient(_rel()), installed=OLD, downloader=bad_download,
        system="Darwin", out=out.append)
    assert rc == 1 and "sha256" in out[-2] and out[-1] == f"stale {NEW}"
    assert _sha_of(app) == OLD


# --- Windows -----------------------------------------------------------------------


def test_windows_runs_the_installer_then_setup_restart_and_prune(tmp_path):
    app = windows_install(tmp_path)
    ancient = add_windows_version(app, "0" * 40)
    calls = []

    def run(argv, **kw):
        argv = [str(a) for a in argv]
        calls.append((argv, kw.get("env")))
        if argv[0].endswith("Setup.exe"):
            ver = add_windows_version(app, NEW)
            (app / "current").unlink()
            (app / "current").symlink_to(ver, target_is_directory=True)
        return subprocess.CompletedProcess(argv, 0, "", "")

    self_update.install_windows(tmp_path / "Setup.exe", NEW, app, tmp_path / "runner.json",
                                run=run, out=lambda *a: None)
    argvs = [a for a, _ in calls]
    assert argvs[0] == [str(tmp_path / "Setup.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                        "/NORESTART", "/UPDATE"]
    assert argvs[1][0] == str(app / "current" / "python" / "python.exe")
    assert argvs[1][1:] == ["-m", "canopy_runner.main", "setup", "--no-pair", "--config",
                            str(tmp_path / "runner.json")]
    assert calls[1][1]["CANOPY_RUNNER_UNDER_JOB"] == "updater"
    assert argvs[2:] == [["schtasks", "/End", "/TN", r"\Canopy\canopy-runner"],
                         ["schtasks", "/Run", "/TN", r"\Canopy\canopy-runner"]]
    assert sorted(p.name for p in (app / "versions").iterdir()) == [OLD[:12], NEW[:12]]
    assert not ancient.exists()


def test_windows_refuses_when_current_names_another_build(tmp_path):
    app = windows_install(tmp_path)
    run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")  # noqa: E731
    with pytest.raises(self_update.UpdateError, match="does not name the expected"):
        self_update.install_windows(tmp_path / "Setup.exe", NEW, app, tmp_path / "r.json",
                                    run=run, out=lambda *a: None)


def test_hold_updates_keeps_a_stale_box_where_it_is(tmp_path):
    app = mac_app(tmp_path / "home")
    hold = tmp_path / "home" / ".canopy" / "hold-updates"
    hold.parent.mkdir(parents=True, exist_ok=True)
    hold.touch()
    out = []
    rc = self_update.run_self_update(
        _config(tmp_path), if_stale=True, executable=mac_python(app), home=tmp_path / "home",
        client_factory=lambda cfg: FakeClient(_rel()), installed=OLD,
        downloader=lambda *a: pytest.fail("no download while held"), system="Darwin",
        out=out.append)
    assert rc == 0 and out[-1] == f"held {NEW}"
    assert _sha_of(app) == OLD
