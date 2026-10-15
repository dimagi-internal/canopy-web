"""The runner's first real Windows run (2026-10-15) found four places that
assumed macOS once the process could start at all:

- the CDP sidecar's `npm install` ran a bare "npm", which CreateProcess cannot
  resolve to `npm.cmd`;
- `pair` wrote `~/Library/Application Support/.../emdash4.db` into runner.json;
- `verify-emdash` read the version only from /Applications, so it always skipped;
- a ✗ printed to a cp1252 console raised UnicodeEncodeError over the real error.

These pin the Windows branch from any host by flipping `platform_jobs.is_windows`
(not `os.name` — see test_platform_jobs.py for why).
"""
from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from canopy_runner import cdp_control, main, pair, platform_jobs, upgrade_check


@pytest.fixture
def on_windows(monkeypatch):
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: True)


def test_app_data_dir_is_appdata_on_windows(on_windows, tmp_path):
    assert platform_jobs.app_data_dir(tmp_path, env={"APPDATA": r"C:\Users\s\AppData\Roaming"}) \
        == Path(r"C:\Users\s\AppData\Roaming")
    assert platform_jobs.app_data_dir(tmp_path, env={}) == tmp_path / "AppData" / "Roaming"


def test_app_data_dir_unchanged_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_jobs, "is_windows", lambda: False)
    assert platform_jobs.app_data_dir(tmp_path, env={"APPDATA": "ignored"}) \
        == tmp_path / "Library" / "Application Support"


def test_pair_finds_the_windows_emdash_db(on_windows, monkeypatch, tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    (appdata / "Emdash").mkdir(parents=True)
    (appdata / "Emdash" / "emdash4.db").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(appdata))
    assert pair.find_emdash_db(tmp_path) == appdata / "Emdash" / "emdash4.db"


def test_sidecar_install_runs_the_resolved_npm(monkeypatch):
    monkeypatch.setattr(cdp_control, "sidecar_deps_installed",
                        lambda: bool(seen))
    monkeypatch.setattr(cdp_control.shutil, "which",
                        lambda name: r"C:\Program Files\nodejs\npm.cmd" if name == "npm" else None)
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cdp_control.subprocess, "run", fake_run)
    cdp_control.ensure_sidecar_deps()
    assert seen[0][0] == r"C:\Program Files\nodejs\npm.cmd"


def test_windows_version_comes_from_the_installer_registry():
    entries = lambda: iter([("Git", "2.44"), ("Emdash 1.2.3", "1.2.3")])  # noqa: E731
    assert upgrade_check.installed_version(windows=True, entries=entries) == "1.2.3"
    assert upgrade_check.installed_version(windows=True, entries=lambda: iter([])) is None


def test_unencodable_mark_does_not_crash_a_cp1252_console():
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    main._tolerant_console([console])
    console.write("✗ the real error")
    console.flush()
    assert raw.getvalue() == b"\\u2717 the real error"
