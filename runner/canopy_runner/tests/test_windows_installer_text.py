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


def _dev_ps1() -> str:
    return (RUNNER / "canopy_runner" / "scripts" / "install-runner.ps1").read_text(
        encoding="utf-8")


def test_developer_install_stops_the_runner_before_replacing_its_files():
    """`uv tool install --force` over a live runner is os error 32 on Windows: the install
    fails, the old daemon heartbeats the old sha, and -IfStale reports current."""
    ps1 = _dev_ps1()
    install = ps1.index("& uv tool install --force")
    stop = ps1.rindex("Stop-RunnerTask", 0, install)
    assert ps1.index("function Stop-RunnerTask") < stop < install
    # Stop waits for the runner's PROCESSES, not just the task state.
    body = ps1[ps1.index("function Stop-RunnerTask"):ps1.index("function Start-RunnerTask")]
    assert "Get-RunnerProcesses" in body and "Stop-Process" in body


def test_developer_install_restarts_the_runner_on_every_failure_path():
    ps1 = _dev_ps1()
    fail = ps1[ps1.index("function Fail"):ps1.index("function Info")]
    assert fail.index("Start-RunnerTask") < fail.index("Write-Error")
    trap = ps1[ps1.index("trap {"):]
    assert trap.index("Start-RunnerTask") < trap.index("break")
    no_tasks = ps1[ps1.index("if ($NoTasks) {"):]
    assert no_tasks.index("Start-RunnerTask") < no_tasks.index("exit 0")


def test_developer_install_hands_over_to_the_archived_installer():
    """The updater runs a copy frozen at the last SUCCESSFUL install, refreshed only after
    `uv tool install`. A frozen copy that fails before then can never replace itself, so
    a fix to the installer never arrives (the pre-#2679 copy hit os error 32 for weeks).
    Like install-runner.sh: once the ref is extracted, run the ARCHIVED installer if it
    differs, before anything is stamped, stopped or installed."""
    ps1 = _dev_ps1()
    extract = ps1.index("& $sysTar -x -f $tar -C $Tmp")
    guard = ps1.index("if (-not $env:CANOPY_INSTALLER_STAGE2)")
    handover = ps1[guard:ps1.index("_build_info.py", guard)]
    assert extract < guard < ps1.index("Stop-RunnerTask\n", guard) < ps1.index(
        "& uv tool install --force", guard)
    assert '"runner/canopy_runner/scripts/install-runner.ps1"' in handover
    assert "(Get-FileHash $PSCommandPath).Hash" in handover
    # Recursion guard set for the child; caller's args passed through; child's exit code kept.
    assert handover.index('$env:CANOPY_INSTALLER_STAGE2 = "1"') < handover.index("-File $stage2Tmp @passArgs")
    assert "$PSBoundParameters" in handover and "exit $code" in handover
    # The copy must outlive the finally that deletes $Tmp.
    assert "GetTempPath()" in handover and "Copy-Item -Path $archived -Destination $stage2Tmp" in handover


def test_update_check_reads_stdout_only():
    """2>&1 under ErrorActionPreference Stop: one stderr line ends the updater on PS 5.1."""
    ps1 = (RUNNER / "canopy_runner" / "scripts" / "install-runner.ps1").read_text(
        encoding="utf-8")
    check = ps1.index("update-check --config $Config 2>$null")
    relax = ps1.rindex('$ErrorActionPreference = "Continue"', 0, check)
    restore = ps1.index("$ErrorActionPreference = $prevEap", check)
    assert relax < check < restore
    assert "update-check --config $Config 2>&1" not in ps1
