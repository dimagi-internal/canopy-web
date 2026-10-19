"""`canopy-runner setup` — what the downloadable installer runs, and what an update re-runs.

In order: (macOS) link `~/.canopy/bin/canopy-device-key` to the helper inside the app, so
the device key lives where canopy's own tooling looks for it; pair from the browser if
this computer has no `runner.json` yet (`browser_pair`); then render and (re)load the
jobs that keep the runner running, all pointing INTO the bundle, and start the runner.

It is the bundled counterpart of `install-runner.sh` / `.ps1`, which stay the developer
path — so it refuses on a developer install rather than pointing a launchd job at a
`uv tool` venv. Idempotent: running it twice changes nothing the second time but a
restart of the runner, which is what an update wants.

Every path it writes is version-free, so a job survives the next update without being
rewritten: the macOS app lives at one place (`~/Applications/Canopy Runner.app`) and is
replaced in place; Windows tasks run through the `current` junction, never through
`versions\\<sha12>`.

**Never bounce the job we are running under.** `self-update` runs as the updater job and
runs THIS command from the new bundle. `launchctl bootout` of the updater job would tear
down that whole process tree — this command included — leaving the timer UNLOADED, which
install-runner.sh learned the hard way. So under the updater (launchd tells us with
`XPC_SERVICE_NAME`; `self-update` also sets `CANOPY_RUNNER_UNDER_JOB=updater`), the
updater's definition is written and left for the next load, and on Windows its task is
re-registered only if its definition actually changed.
"""
from __future__ import annotations

import json
import os
import platform as _platform
import plistlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from . import bundle, platform_jobs

UNDER_JOB_ENV = "CANOPY_RUNNER_UNDER_JOB"
MENUBAR_LABEL = platform_jobs.MENUBAR_LABEL
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
#: Seconds between updater runs (the 30-minute timer install-runner.sh also uses).
UPDATER_INTERVAL = 1800

NOT_BUNDLED = ("This is not an installed Canopy Runner app, so there is nothing for setup to "
               "point the background jobs at. Developer installs are set up with "
               "runner/canopy_runner/scripts/install-runner.sh (or install-runner.ps1).")


class SetupError(Exception):
    """A step failed; the message is for a person."""


def under_updater(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(UNDER_JOB_ENV) == "updater"
            or env.get("XPC_SERVICE_NAME") == platform_jobs.UPDATER_LABEL)


# --- macOS -----------------------------------------------------------------------


def link_helper(root: Path, home: Path, *, out=print) -> str:
    """~/.canopy/bin/canopy-device-key → the app's helper. Replaces a stale file or link;
    never touches the key files themselves (they live in ~/.canopy/device-key)."""
    target = root / "bin" / "canopy-device-key"
    link = home / ".canopy" / "bin" / "canopy-device-key"
    if not target.exists():
        return "skipped (the app carries no canopy-device-key)"
    try:
        if link.is_symlink() and os.readlink(link) == str(target):
            return "unchanged"
    except OSError:
        pass
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + ".new")
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, link)
    except OSError as exc:
        return f"FAILED ({exc})"
    return f"linked to {target}"


def mac_plists(root: Path, home: Path, config: Path) -> dict[str, dict]:
    """label → plist dict for the three LaunchAgents."""
    canopy = home / ".canopy"
    runner_bin = str(root / "bin" / "canopy-runner")
    path = f"{root / 'node' / 'bin'}:{root / 'acp' / 'bin'}:{BASE_PATH}"
    env = {"PATH": path, "PYTHONUNBUFFERED": "1"}
    jobs = {
        platform_jobs.RUNNER_LABEL: {
            "Label": platform_jobs.RUNNER_LABEL,
            "ProgramArguments": [runner_bin, "run", "--config", str(config)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(canopy / "runner.log"),
            "StandardErrorPath": str(canopy / "runner.log"),
            "EnvironmentVariables": env,
            # Login Items › "App Background Activity" names this job after the app,
            # not after a bare `canopy-runner` script nobody recognises.
            "AssociatedBundleIdentifiers": [MENUBAR_LABEL],
        },
        platform_jobs.UPDATER_LABEL: {
            "Label": platform_jobs.UPDATER_LABEL,
            "ProgramArguments": [runner_bin, "self-update", "--if-stale", "--config",
                                 str(config)],
            "RunAtLoad": False,
            "StartInterval": UPDATER_INTERVAL,
            "StandardOutPath": str(canopy / "updater.log"),
            "StandardErrorPath": str(canopy / "updater.log"),
            "EnvironmentVariables": env,
            "AssociatedBundleIdentifiers": [MENUBAR_LABEL],
        },
    }
    app = bundle.app_path(str(root / "python" / "bin" / "python3"))
    if app is not None:
        jobs[MENUBAR_LABEL] = {
            "Label": MENUBAR_LABEL,
            "ProgramArguments": ["/usr/bin/open", "-g", str(app)],
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }
    return jobs


class Launchd:
    """bootout/bootstrap with install-runner.sh's waits and retries. `run` and `sleep`
    are seams: tests never reach launchctl."""

    def __init__(self, *, run=subprocess.run, sleep: Callable[[float], None] = time.sleep,
                 uid: Optional[int] = None, out=print, env=None,
                 plutil: Optional[str] = None):
        self.run = run
        self.plutil = plutil if plutil is not None else shutil.which("plutil")
        self.sleep = sleep
        self.uid = os.getuid() if uid is None else uid
        self.out = out
        self.env = os.environ if env is None else env

    def _lc(self, *args: str) -> subprocess.CompletedProcess:
        return self.run(["launchctl", *args], capture_output=True, text=True, timeout=30)

    def loaded(self, label: str) -> bool:
        return self._lc("print", f"gui/{self.uid}/{label}").returncode == 0

    def install(self, label: str, plist: dict, dest_dir: Path, *,
                restart_if_unchanged: bool = False) -> bool:
        """Write, validate, (re)load. True when the job is (left) running."""
        data = plistlib.dumps(plist)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{label}.plist"
        staging = dest_dir / f".{label}.plist.new"
        staging.write_bytes(data)
        if self.plutil:
            lint = self.run([self.plutil, "-lint", str(staging)], capture_output=True, text=True,
                            timeout=30)
            if lint.returncode != 0:
                staging.unlink(missing_ok=True)
                raise SetupError(f"the rendered {label} job is not valid — left the running "
                                 "one alone")
        try:
            changed = dest.read_bytes() != data
        except OSError:
            changed = True
        os.replace(staging, dest)

        if label == platform_jobs.UPDATER_LABEL and under_updater(self.env):
            self.out(f"    {label}: {'updated — loads next login' if changed else 'unchanged'} "
                     "(not restarted: this update is running under it)")
            return True
        uid = self.uid
        if not changed and self.loaded(label):
            if restart_if_unchanged:
                self._lc("kickstart", "-k", f"gui/{uid}/{label}")
                self.out(f"    {label}: restarted")
            else:
                self.out(f"    {label}: already running")
            return True
        self._lc("bootout", f"gui/{uid}/{label}")
        for _ in range(20):
            if not self.loaded(label):
                break
            self.sleep(0.5)
        err = ""
        for attempt in range(1, 6):
            p = self._lc("bootstrap", f"gui/{uid}", str(dest))
            if p.returncode == 0:
                self.out(f"    {label}: started")
                return True
            err = (p.stderr or p.stdout or "").strip()
            if attempt < 5:
                self.sleep(1)
        raise SetupError(f"launchd would not start {label} after 5 attempts ({err[:200]}). "
                         f"Retry with: launchctl bootstrap gui/{uid} {dest}")


def setup_macos(root: Path, home: Path, config: Path, *, launchd: Launchd, out=print) -> int:
    agents = home / "Library" / "LaunchAgents"
    jobs = mac_plists(root, home, config)
    (home / ".canopy").mkdir(parents=True, exist_ok=True)
    out("==> starting the runner in the background")
    launchd.install(platform_jobs.RUNNER_LABEL, jobs[platform_jobs.RUNNER_LABEL], agents,
                    restart_if_unchanged=True)
    out("==> turning on automatic updates")
    try:
        launchd.install(platform_jobs.UPDATER_LABEL, jobs[platform_jobs.UPDATER_LABEL], agents)
    except SetupError as exc:
        out(f"    WARNING: {exc} — the runner works, but updates stay manual for now")
    if MENUBAR_LABEL in jobs:
        out("==> adding Canopy Runner to the menu bar (and to Login Items)")
        try:
            launchd.install(MENUBAR_LABEL, jobs[MENUBAR_LABEL], agents)
        except SetupError as exc:
            out(f"    WARNING: {exc}")
    return 0


# --- Windows ---------------------------------------------------------------------


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _cmd_quote(text: str) -> str:
    return f'"{text}"'


def windows_commands(app_dir: Path, home: Path, config: Path) -> dict[str, tuple[str, str]]:
    """task name → (executable, arguments), always through `current`."""
    pyw = app_dir / "current" / "python" / "pythonw.exe"
    canopy = home / ".canopy"
    return {
        "canopy-runner": (str(pyw), " ".join([
            "-m", "canopy_runner.main", "run", "--config", _cmd_quote(str(config)),
            "--log-file", _cmd_quote(str(canopy / "runner.log"))])),
        "canopy-runner-updater": (str(pyw), " ".join([
            "-m", "canopy_runner.main", "self-update", "--if-stale", "--config",
            _cmd_quote(str(config)), "--log-file", _cmd_quote(str(canopy / "updater.log"))])),
    }


def register_script(name: str, exe: str, args: str, *, updater: bool) -> str:
    """PowerShell that registers \\Canopy\\<name> exactly as install-runner.ps1 does: the
    current user's identity from the process token, Interactive, Limited, and an AtLogOn
    trigger that names the user (without -User it means "any user", which only an
    administrator may register)."""
    if updater:
        trigger = ("New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(30) "
                   "-RepetitionInterval (New-TimeSpan -Minutes 30)")
        settings = ("New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
                    "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew")
    else:
        trigger = "New-ScheduledTaskTrigger -AtLogOn -User $u"
        settings = ("New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
                    "-DontStopIfGoingOnBatteries -RestartCount 999 "
                    "-RestartInterval (New-TimeSpan -Minutes 1) "
                    "-ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew")
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$u = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
        f"$a = New-ScheduledTaskAction -Execute {_ps_quote(exe)} -Argument {_ps_quote(args)}",
        "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited",
        f"$t = {trigger}",
        f"$s = {settings}",
        f"Register-ScheduledTask -TaskName {_ps_quote(name)} -TaskPath '\\Canopy' -Action $a "
        "-Trigger $t -Principal $p -Settings $s -Force | Out-Null",
    ])


def _stamp_path(home: Path) -> Path:
    return home / ".canopy" / "updater-task.json"


def setup_windows(root: Path, home: Path, config: Path, *, run=subprocess.run, out=print,
                  env=None) -> int:
    app_dir = bundle.windows_app_dir(str(root / "python" / "python.exe"))
    if app_dir is None:
        raise SetupError(f"{root} is not inside a Canopy Runner install (no versions\\ or "
                         "current above it) — reinstall Canopy Runner")
    (home / ".canopy").mkdir(parents=True, exist_ok=True)
    cmds = windows_commands(app_dir, home, config)

    def register(name: str, updater: bool) -> None:
        exe, args = cmds[name]
        p = run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 register_script(name, exe, args, updater=updater)],
                capture_output=True, text=True, timeout=120, **platform_jobs.no_window())
        if p.returncode != 0:
            err = (p.stderr or p.stdout or "").strip()
            if "Access is denied" in err or "0x80070005" in err:
                raise SetupError(
                    f"Task Scheduler refused to register \\Canopy\\{name} (Access is denied). "
                    "Run the installer from an ordinary, non-administrator session. If an earlier "
                    "install ran as administrator, remove its tasks once from an elevated "
                    "PowerShell — Unregister-ScheduledTask -TaskPath '\\Canopy\\' -Confirm:$false "
                    "— then run setup again.")
            raise SetupError(f"could not register \\Canopy\\{name}: {err[-300:]}")
        out(f"    \\Canopy\\{name}: registered")

    out("==> setting the runner to start when you sign in")
    register("canopy-runner", updater=False)
    out("==> turning on automatic updates")
    stamp = _stamp_path(home)
    want = json.dumps(list(cmds["canopy-runner-updater"]))
    try:
        have = stamp.read_text(encoding="utf-8")
    except OSError:
        have = ""
    if under_updater(env) and have == want:
        out("    \\Canopy\\canopy-runner-updater: unchanged (this update is running under it)")
    else:
        try:
            register("canopy-runner-updater", updater=True)
            stamp.write_text(want, encoding="utf-8")
        except SetupError as exc:
            out(f"    WARNING: {exc} — the runner works, but updates stay manual for now")
    out("==> starting the runner")
    run(["schtasks", "/Run", "/TN", platform_jobs.WINDOWS_RUNNER_TASK],
        capture_output=True, text=True, timeout=30, **platform_jobs.no_window())
    return 0


# --- the command -----------------------------------------------------------------


def run_setup(config_path: Path, *, no_pair: bool = False, base_url: str = "",
              allow_software: Optional[bool] = None, home: Optional[Path] = None,
              executable: Optional[str] = None, system: Optional[str] = None,
              run=subprocess.run, sleep=time.sleep, pair_fn=None, out=print,
              env=None, uid: Optional[int] = None) -> int:
    from . import pair

    home = home or Path.home()
    config_path = Path(config_path).expanduser()
    system = system or _platform.system()
    root = bundle.root(executable)
    if root is None:
        out(NOT_BUNDLED)
        return 2
    if system not in ("Darwin", "Windows"):
        out("Canopy Runner's installer supports macOS and Windows only. On Linux, install a "
            "runner with runner/canopy_runner/scripts/install-runner.sh.")
        return 1

    out("Setting up Canopy Runner on this computer.")
    if system == "Darwin":
        out(f"==> device-key helper: {link_helper(root, home, out=out)}")

    if not config_path.exists():
        if no_pair:
            out(f"This computer is not paired yet ({config_path} is missing). Run "
                "`canopy-runner setup` (without --no-pair) to pair it from your browser.")
            return 1
        out("==> pairing this computer with Canopy (your browser will open)")
        if pair_fn is None:
            from .browser_pair import run_browser_pair as pair_fn
        try:
            pair_fn(config_path, base_url=base_url or pair.DEFAULT_BASE_URL,
                    allow_software=allow_software, home=home)
        except pair.PairError as exc:
            out(f"Pairing did not finish: {exc}")
            return 1

    try:
        if system == "Darwin":
            rc = setup_macos(root, home, config_path, out=out,
                             launchd=Launchd(run=run, sleep=sleep, uid=uid, out=out, env=env))
        else:
            rc = setup_windows(root, home, config_path, run=run, out=out, env=env)
    except SetupError as exc:
        out(f"Setup did not finish: {exc}")
        return 1
    out("Done. Canopy Runner is running and will keep itself up to date.")
    return rc
