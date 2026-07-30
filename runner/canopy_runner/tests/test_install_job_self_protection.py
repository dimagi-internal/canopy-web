"""install_job must never bootout the job it is running under.

`launchctl bootout` tears down a job's whole process tree. In --if-stale mode
that tree contains install-runner.sh itself, so booting out the updater label
kills the installer before it can bootstrap the job back — leaving auto-update
unloaded until the next login. Observed 2026-07-29; see the comment above
install_job.

These drive the REAL shell function with a fake `launchctl` on PATH and assert
on the calls it makes, because the bug is entirely in which subcommands run
against which label — invisible to any amount of reading the script.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-runner.sh"

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>%s</string>
  <key>ProgramArguments</key><array><string>/bin/true</string></array>
</dict></plist>
"""

UPDATER = "com.canopy.runner.updater"
RUNNER = "com.canopy.runner"


def _extract(name: str, *, required: bool = True) -> str:
    """Pull one top-level function out of the installer.

    The script is a linear program (it archives a git ref, builds wheels, talks
    to launchd), so it cannot simply be sourced. Anchoring on `^name()` .. `^}`
    is exact for this file: every function in it is written at column zero.

    A missing optional helper yields a stub rather than an error, so these stay
    tests of BEHAVIOUR: point them at a build without the fix and the assertions
    fail on the bootout it performs, instead of erroring on a name they expected
    to find. A test that only proves a function exists proves nothing about it.
    """
    src = SCRIPT.read_text()
    marker = f"\n{name}() {{\n"
    if marker not in src:
        if required:
            raise AssertionError(f"{name}() not found in {SCRIPT}")
        return f"{name}() {{ return 1; }}\n"
    start = src.index(marker) + 1
    end = src.index("\n}\n", start) + 3
    return src[start:end]


@pytest.fixture(scope="module")
def harness() -> str:
    if shutil.which("bash") is None:  # pragma: no cover - macOS/Linux both have it
        pytest.skip("bash not available")
    return _extract("self_job", required=False) + "\n" + _extract("install_job")


def _run(tmp_path: Path, harness: str, *, label: str, if_stale: int,
         plist_body: str | None = None, preexisting: str | None = None) -> tuple[str, list[str]]:
    """Invoke install_job for `label` and return (stdout, launchctl calls)."""
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    if preexisting is not None:
        (home / "Library" / "LaunchAgents" / f"{label}.plist").write_text(preexisting)

    src = tmp_path / "rendered.plist"
    src.write_text(plist_body if plist_body is not None else PLIST % label)

    calls = tmp_path / "launchctl-calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Fake launchctl: records argv, and reports the job as absent so the real
    # bootstrap path (the one we must NOT reach for our own job) succeeds.
    (bin_dir / "launchctl").write_text(
        f'#!/bin/bash\necho "$@" >> {calls}\n[ "$1" = "print" ] && exit 1\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    # plutil is macOS-only and this suite also runs on Linux CI.
    (bin_dir / "plutil").write_text("#!/bin/bash\nexit 0\n")
    (bin_dir / "plutil").chmod(0o755)

    script = (
        f'IF_STALE={if_stale}\n'
        f'UPDATER_LABEL="{UPDATER}"\n'
        f'{harness}\n'
        f'install_job "{label}" "{src}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    made = calls.read_text().splitlines() if calls.exists() else []
    return proc.stdout, made


def test_the_updater_does_not_bootout_itself(tmp_path, harness):
    """The regression: in --if-stale mode the updater IS the running process."""
    out, calls = _run(tmp_path, harness, label=UPDATER, if_stale=1)
    assert not any(c.startswith("bootout") for c in calls), calls
    assert not any(c.startswith("bootstrap") for c in calls), calls
    assert "left running" in out or "loads at next login" in out


def test_the_updater_plist_is_still_written(tmp_path, harness):
    """Not bouncing must not mean not updating: the new definition still lands
    on disk, so it takes effect at the next load."""
    body = PLIST % UPDATER + "<!-- new -->\n"
    _run(tmp_path, harness, label=UPDATER, if_stale=1,
         plist_body=body, preexisting=PLIST % UPDATER)
    dest = tmp_path / "home" / "Library" / "LaunchAgents" / f"{UPDATER}.plist"
    assert dest.read_text() == body


def test_a_changed_definition_says_so(tmp_path, harness):
    """The operator needs to know a login is pending for it to take effect —
    'unchanged' and 'updated, deferred' are different situations."""
    out, _ = _run(tmp_path, harness, label=UPDATER, if_stale=1,
                  plist_body=PLIST % UPDATER + "<!-- new -->\n",
                  preexisting=PLIST % UPDATER)
    assert "loads at next login" in out


def test_the_runner_job_is_still_bounced_by_the_updater(tmp_path, harness):
    """Only the updater's OWN label is protected. The whole point of an update
    is to restart the runner onto the new code, and that must keep happening."""
    _, calls = _run(tmp_path, harness, label=RUNNER, if_stale=1)
    assert any(c.startswith("bootout gui/") and RUNNER in c for c in calls), calls
    assert any(c.startswith("bootstrap") for c in calls), calls


def test_a_manual_install_still_bounces_the_updater(tmp_path, harness):
    """Without --if-stale nothing is running under the updater job, so a hand-run
    install must still restart it — that is what re-arms a box the old bug left
    unloaded."""
    _, calls = _run(tmp_path, harness, label=UPDATER, if_stale=0)
    assert any(c.startswith("bootout gui/") and UPDATER in c for c in calls), calls
    assert any(c.startswith("bootstrap") for c in calls), calls
