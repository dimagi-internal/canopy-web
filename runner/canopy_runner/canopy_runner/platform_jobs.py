"""The ONE place the runner knows which OS supervises it.

Nearly everything else in `canopy_runner` is platform-neutral — the producers
(`inbox.py`, `schedules.py`) shell out to `gog` and do date math, `cdp_control`
drives emdash through `node`, and the transcript/tail layer is pure pathlib. The
OS-specific things the runner does are **ask its supervisor to run the updater
job now** (here), and **render the collision dialog** (`dialog.py`, which carries
its own osascript/PowerShell split for the same reason). This one was written
inline against `launchctl`, which made the whole daemon look macOS-only when one
function was.

Say "nearly": this docstring used to claim everything else WAS neutral, and
`dialog.py` was osascript-only the whole time it said so — on Windows the human
was never asked and the message was silently dropped. A confident inventory is
the thing that stops anyone re-checking, so keep this list honest or drop it.

So the split lives here rather than as `if sys.platform` scattered at call
sites: a second OS is a new branch in one function with one test, not an audit.

| | macOS | Windows |
|---|---|---|
| supervisor | launchd | Task Scheduler |
| runner job | `com.canopy.runner` | `\\Canopy\\canopy-runner` |
| updater job | `com.canopy.runner.updater` | `\\Canopy\\canopy-runner-updater` |
| run-now verb | `launchctl kickstart gui/<uid>/<label>` | `schtasks /Run /TN <task>` |

`os.getuid()` does not exist on Windows, which is why it is referenced only
inside the POSIX branch — a module-level `os.getuid()` would make this module
unimportable there, and the import happens long before any kickstart.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: launchd labels (macOS). Also the filenames of the two plist templates.
UPDATER_LABEL = "com.canopy.runner.updater"
RUNNER_LABEL = "com.canopy.runner"
#: The menu-bar app's LaunchAgent — and its bundle identifier, which the runner and
#: updater jobs name as AssociatedBundleIdentifiers (setup_cmd).
MENUBAR_LABEL = "com.canopy.runner.menubar"

#: Task Scheduler paths (Windows). Backslash-prefixed = the task folder, so both
#: jobs group under a "Canopy" folder in taskschd.msc rather than littering root.
WINDOWS_UPDATER_TASK = r"\Canopy\canopy-runner-updater"
WINDOWS_RUNNER_TASK = r"\Canopy\canopy-runner"


def is_windows() -> bool:
    """`os.name`, not `sys.platform`: the question is 'does this box have
    launchctl or schtasks', which is exactly the posix/nt split."""
    return os.name == "nt"


def process_account() -> str:
    """The account this process RUNS AS, from the OS, never from the environment.

    Not ``getpass.getuser()``: that reads LOGNAME/USER/USERNAME first, and a
    session launched with LOGNAME=root once pinned a runner to an account that
    does not exist (#1008). POSIX asks the uid; Windows asks GetUserNameW (what
    ``os.getlogin()`` calls there), which reads the process token. ``pwd`` is
    imported inside the POSIX branch for the same reason ``os.getuid()`` is
    above: it does not exist on Windows, and a module-level import made every
    canopy-runner command die with ModuleNotFoundError before it started.
    """
    if is_windows():
        return os.getlogin()
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def app_data_dir(home: Path, *, env=None) -> Path:
    """Where per-user apps keep their data on THIS box: emdash (Electron's
    `appData`) and gog (Go's `os.UserConfigDir`) both resolve here.

    macOS is `~/Library/Application Support`; Windows is `%APPDATA%` (Roaming).
    Every other caller wrote the macOS path inline, so on Windows `pair` wrote a
    runner.json pointing at a database that could not exist and the gmail watch
    looked for gog credentials in a directory nobody creates (2026-10-15, the first
    real Windows runner). Non-Windows keeps the macOS layout, unchanged.
    """
    env = os.environ if env is None else env
    if is_windows():
        appdata = env.get("APPDATA")
        return Path(appdata) if appdata else home / "AppData" / "Roaming"
    return home / "Library" / "Application Support"


def updater_job_name() -> str:
    """What the updater job is called on THIS box — for log lines and errors."""
    return WINDOWS_UPDATER_TASK if is_windows() else UPDATER_LABEL


def kickstart_updater(*, runner=subprocess.run) -> None:
    """Ask the supervisor to run the updater job NOW.

    Raises on failure (CalledProcessError / OSError / TimeoutExpired); the sole
    caller, `update.nudge`, swallows and logs it, because this runs on the wake
    listener thread that also carries cancel and wake frames.
    """
    if is_windows():
        cmd = ["schtasks", "/Run", "/TN", WINDOWS_UPDATER_TASK]
    else:
        cmd = ["launchctl", "kickstart", f"gui/{os.getuid()}/{UPDATER_LABEL}"]
    runner(cmd, capture_output=True, timeout=15, check=True)


def no_window() -> dict:
    """subprocess kwargs that keep a child from opening a console window on Windows.

    The bundled runner and updater run under pythonw.exe, which has no console, so every
    console program they start (powershell, schtasks, python.exe) would otherwise flash
    a window of its own in front of whoever is using the computer. Empty elsewhere."""
    return {"creationflags": 0x08000000} if is_windows() else {}  # CREATE_NO_WINDOW


#: How long `restart_windows_task` waits for /End to settle, and then for /Run to take.
TASK_SETTLE_SECONDS = 15
TASK_POLL_SECONDS = 0.5


def windows_task_running(task: str, *, run=subprocess.run) -> bool:
    """Is `task` Running right now? `schtasks /Query /FO CSV /NH` prints one line,
    `"<task>","<next run>","<status>"`. Any failure to ask reads as not running."""
    try:
        p = run(["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10, **no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    if p.returncode != 0:
        return False
    for line in (p.stdout or "").splitlines():
        if line.rsplit(",", 1)[-1].strip().strip('"') == "Running":
            return True
    return False


def restart_windows_task(task: str, *, run=subprocess.run, sleep=None,
                         settle: float = TASK_SETTLE_SECONDS) -> bool:
    """/End `task`, wait until it is no longer Running, /Run it, and confirm it runs.

    The tasks are `-MultipleInstances IgnoreNew`: a /Run that lands while /End is
    still settling is DROPPED, and the daemon stays down (until the 5-minute repeat
    brings it back). So wait for /End to finish before /Run, then check /Run took.
    True when the task is Running at the end."""
    import time

    sleep = time.sleep if sleep is None else sleep
    tries = max(1, int(settle / TASK_POLL_SECONDS))
    kw = dict(capture_output=True, text=True, timeout=30, **no_window())
    run(["schtasks", "/End", "/TN", task], **kw)
    for _ in range(tries):
        if not windows_task_running(task, run=run):
            break
        sleep(TASK_POLL_SECONDS)
    run(["schtasks", "/Run", "/TN", task], **kw)
    for _ in range(tries):
        if windows_task_running(task, run=run):
            return True
        sleep(TASK_POLL_SECONDS)
    return False
