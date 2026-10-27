"""The Windows installer's own text: the .iss, its Start-menu scripts and the developer
install-runner.ps1. CI has no Windows and no Inno Setup, so what cannot be run is pinned."""
from __future__ import annotations

import re
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[2]
WIN = RUNNER / "packaging" / "windows"
ISS = (WIN / "canopy-runner.iss").read_text(encoding="utf-8")


def _section(name: str) -> str:
    m = re.search(rf"^\[{name}\]\n(.*?)(?=^\[|\Z)", ISS, re.M | re.S)
    assert m, f"no [{name}] section"
    return m.group(1)


def test_a_failed_junction_flip_restores_the_previous_version():
    code = _section("Code")
    flip = code.index("mklink /J \"' + Cur + '\" \"' + Target")
    fail = code.index("RaiseException('Could not point", flip)
    between = code[flip:fail]
    assert "PreviousSha" in between and "mklink /J \"' + Cur + '\" \"' + Prev" in between


def test_start_menu_has_log_restart_and_stop():
    icons = _section("Icons")
    assert "{autoprograms}\\Canopy Runner\\Canopy Runner – Open log" in icons
    assert "notepad.exe" in icons and "{%USERPROFILE}\\.canopy\\runner.log" in icons
    assert "Canopy Runner – Restart\"; Filename: \"{app}\\restart-runner.cmd\"" in icons
    assert "Canopy Runner – Stop\"; Filename: \"{app}\\stop-runner.cmd\"" in icons
    files = _section("Files")
    for script in ("restart-runner.cmd", "stop-runner.cmd"):
        assert f'Source: "{script}"; DestDir: "{{app}}"' in files
        assert (WIN / script).read_bytes().count(b"\r\n") > 2, "CRLF, for cmd.exe"


def test_restart_script_waits_for_end_before_run():
    text = "\n".join(ln for ln in (WIN / "restart-runner.cmd").read_text().splitlines()
                     if not ln.startswith("rem "))
    end, query, run = (text.index(s) for s in ("/End", "Get-ScheduledTask", "/Run"))
    assert "schtasks /Query" not in text, "its status column is translated"
    assert end < query < run and "/ENABLE" in text
    stop = (WIN / "stop-runner.cmd").read_text()
    assert "/DISABLE" in stop and "/End" in stop


def test_the_pruning_copies_point_at_each_other():
    assert "self_update.prune_versions" in _section("Code")


def test_developer_install_waits_for_end_before_run():
    ps1 = (RUNNER / "canopy_runner" / "scripts" / "install-runner.ps1").read_text(
        encoding="utf-8")
    end = ps1.index('schtasks /End /TN "$TASK_FOLDER\\$RUNNER_TASK"')
    query = ps1.index('Get-ScheduledTask -TaskPath "$TASK_FOLDER\\" -TaskName $RUNNER_TASK', end)
    run = ps1.index('schtasks /Run /TN "$TASK_FOLDER\\$RUNNER_TASK"', query)
    assert end < query < run
