"""The supervisor split: launchd on macOS, Task Scheduler on Windows.

The runner was macOS-only for one reason — it asked `launchctl` to run the
updater job, inline, with `os.getuid()` in the argv. Everything else in the
package is platform-neutral. These tests pin BOTH branches from either host, so
the Windows path cannot rot untested on a fleet of Macs:

- the command is built for the right supervisor,
- `os.getuid()` is never touched on the Windows branch (it does not exist
  there, so a stray reference would raise at kickstart time — and the sole
  caller swallows exceptions, so it would surface as a runner that silently
  never auto-updates rather than as a crash),
- the job NAME the log line reports matches the platform it ran on.

They flip `platform_jobs.is_windows`, NOT `os.name`. Patching `os.name` globally
is what a first cut did, and it breaks the interpreter under the test: `pathlib`
selects WindowsPath/PosixPath off `os.name`, so pytest's own cachedir handling
dies with "cannot instantiate 'WindowsPath' on your system" before any assertion
runs. The seam is the function.
"""
from __future__ import annotations

import pytest

from canopy_runner import platform_jobs


@pytest.fixture
def calls():
    seen = []

    def fake(cmd, **kw):
        seen.append((cmd, kw))

    return seen, fake


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: True)


@pytest.fixture
def on_posix(monkeypatch):
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: False)
    monkeypatch.setattr(platform_jobs.os, "getuid", lambda: 501, raising=False)


def test_posix_kickstart_uses_launchctl_with_the_gui_domain(on_posix, calls):
    seen, fake = calls
    platform_jobs.kickstart_updater(runner=fake)

    (cmd, kw), = seen
    assert cmd == ["launchctl", "kickstart", "gui/501/com.canopy.runner.updater"]
    assert kw["check"] is True and kw["timeout"] == 15


def test_windows_kickstart_uses_schtasks(on_windows, calls):
    seen, fake = calls
    platform_jobs.kickstart_updater(runner=fake)

    (cmd, kw), = seen
    assert cmd == ["schtasks", "/Run", "/TN", r"\Canopy\canopy-runner-updater"]
    assert kw["check"] is True and kw["timeout"] == 15


def test_windows_branch_never_calls_getuid(on_windows, monkeypatch, calls):
    """`os.getuid` is absent on Windows. Deleting it reproduces that, so a
    regression that hoists the uid out of the POSIX branch fails loudly."""
    seen, fake = calls
    monkeypatch.delattr(platform_jobs.os, "getuid", raising=False)

    platform_jobs.kickstart_updater(runner=fake)  # must not raise

    assert seen[0][0][0] == "schtasks"


def test_updater_job_name_on_windows(on_windows):
    assert platform_jobs.updater_job_name() == r"\Canopy\canopy-runner-updater"


def test_updater_job_name_on_posix(on_posix):
    assert platform_jobs.updater_job_name() == "com.canopy.runner.updater"


def test_is_windows_reads_os_name():
    """The real predicate, unpatched — posix/nt is exactly the launchctl/schtasks
    split, which is why it is `os.name` and not `sys.platform`."""
    import os

    assert platform_jobs.is_windows() is (os.name == "nt")


def test_nudge_reports_the_platform_job_name(on_windows, monkeypatch, tmp_path, caplog):
    """End of the wire: `update.nudge` logs the Windows task path on Windows,
    not the launchd label it used to hardcode."""
    import logging

    from canopy_runner import provenance, update

    monkeypatch.setattr(provenance, "code_sha", lambda: "a" * 40)
    monkeypatch.setattr(update, "_kickstart_updater", lambda: None)
    monkeypatch.setattr(update, "_last_nudge_at", 0.0)

    cfg = type("Cfg", (), {"state_dir": tmp_path})()
    with caplog.at_level(logging.INFO, logger="canopy_runner.update"):
        assert update.nudge(cfg, "b" * 40, now=10_000.0) is True

    assert any(r"\Canopy\canopy-runner-updater" in m for m in caplog.messages)
