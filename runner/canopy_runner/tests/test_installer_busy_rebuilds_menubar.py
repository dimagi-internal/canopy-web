"""A busy box still gets the menu-bar app the deploy expects.

`update-check` answers `busy` while any turn is in flight, and on a box that always
has a turn in flight the runner was never updated — and the app with it, though
rebuilding the app restarts only the app. So on `busy` the updater builds the app
from the expected sha and leaves the daemon alone.

Drives the REAL script against a throwaway git repo, with fakes on PATH for the
installed runner (`update-check`), `uv`, `uname`, `swiftc` and `launchctl`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="bash install script (macOS/Linux only)")

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-runner.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _fake(bin_dir: Path, name: str, body: str) -> None:
    (bin_dir / name).write_text("#!/bin/bash\n" + body)
    (bin_dir / name).chmod(0o755)


@pytest.fixture
def box(tmp_path: Path):
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git not available")
    home, repo, bin_dir, tools = (tmp_path / d for d in ("home", "repo", "bin", "tools"))
    for d in (home / ".canopy", repo, bin_dir, tools):
        d.mkdir(parents=True)
    built = tmp_path / "built"
    calls = tmp_path / "launchctl-calls"

    # The repo the updater archives from: a menu-bar app whose build.sh records the run.
    (repo / "runner" / "menubar" / "Sources").mkdir(parents=True)
    (repo / "runner" / "menubar" / "Sources" / "main.swift").write_text("// app\n")
    (repo / "runner" / "menubar" / "build.sh").write_text(f'echo built >> "{built}"\n')
    (repo / "assets" / "brand").mkdir(parents=True)
    (repo / "assets" / "brand" / "AppIcon.icns").write_text("icon")
    (repo / "assets" / "brand" / "menubar-mark.png").write_text("mark")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "app")
    sha = _git(repo, "rev-parse", "HEAD")

    (home / ".canopy" / "runner.json").write_text("{}")
    _fake(tools, "canopy-runner", f'echo "busy {sha}"\n')
    _fake(bin_dir, "uv", f'[ "$1 $2" = "tool dir" ] && echo "{tools}"\nexit 0\n')
    _fake(bin_dir, "uname", 'echo Darwin\n')
    _fake(bin_dir, "swiftc", 'exit 0\n')
    _fake(bin_dir, "launchctl", f'echo "$@" >> "{calls}"\nexit 0\n')

    def run():
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--if-stale", "--repo", str(repo),
             "--config", str(home / ".canopy" / "runner.json")],
            capture_output=True, text=True, timeout=60,
            env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout
    return run, built, calls, home


def test_busy_rebuilds_the_app_and_leaves_the_daemon_alone(box):
    run, built, calls, home = box
    out = run()
    assert "busy" in out
    assert built.read_text().count("built") == 1
    assert (home / ".canopy" / "menubar.sha").read_text().strip()
    # No daemon or updater job touched: the runner waits for its turns to finish.
    assert not calls.exists()


def test_busy_does_not_rebuild_an_app_that_is_already_current(box):
    run, built, _calls, home = box
    run()
    (home / "Applications" / "Canopy Runner.app").mkdir(parents=True)
    out = run()
    assert "menu-bar app unchanged" in out
    assert built.read_text().count("built") == 1
