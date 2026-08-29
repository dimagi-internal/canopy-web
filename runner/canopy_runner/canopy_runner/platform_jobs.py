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

#: launchd labels (macOS). Also the filenames of the two plist templates.
UPDATER_LABEL = "com.canopy.runner.updater"
RUNNER_LABEL = "com.canopy.runner"

#: Task Scheduler paths (Windows). Backslash-prefixed = the task folder, so both
#: jobs group under a "Canopy" folder in taskschd.msc rather than littering root.
WINDOWS_UPDATER_TASK = r"\Canopy\canopy-runner-updater"
WINDOWS_RUNNER_TASK = r"\Canopy\canopy-runner"


def is_windows() -> bool:
    """`os.name`, not `sys.platform`: the question is 'does this box have
    launchctl or schtasks', which is exactly the posix/nt split."""
    return os.name == "nt"


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
