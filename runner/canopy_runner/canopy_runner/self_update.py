"""`canopy-runner self-update` — the updater of a runner installed from the download.

A developer install updates through `install-runner.sh --if-stale` (git archive, `uv tool
install`). A bundled install has neither git nor uv, so it updates the way an app does:
ask canopy-web which build is current, download it, check it, swap it in, restart.

The decision is `update.update_status`'s, with the same `current|stale|busy|unknown`
vocabulary and the same rules — only the EXPECTED side comes from somewhere else:
`GET /api/harness/runner-releases/current?platform=<p>` with this runner's own token,
which names the deployed `RUNNER_CODE_SHA` AND a downloadable artifact for it. So:

- 404 (no release built for the deployed sha), any error, an unparseable answer → unknown,
  exit 0. It runs on a timer; "nothing to do" must not look like a failure.
- equal sha → current; a turn in flight → busy (an update restarts the runner, and a
  chat turn bridged across ticks would lose its reply); else stale → install.
- It never installs any sha but the server's answer, checks the download's sha256 and
  size against that answer, and checks the installed bundle's release.json names it too.

macOS replaces `~/Applications/Canopy Runner.app` in place: unpack beside it, verify the
code signature (and, when the running app is signed, that the new one is signed by the
SAME team — a different team is a different publisher, whatever the sha256 says), rename
the old app aside, rename the new one in, and only then run the NEW bundle's `setup
--no-pair`, which reloads the jobs and restarts the runner. Any failure before the swap
leaves the old app exactly where it was; the swap itself is two renames with a rollback
between them, so there is never a moment with no app.

Windows runs the downloaded Setup with `/VERYSILENT … /UPDATE`, which writes
`versions\\<sha12>` and flips the `current` junction; then the new bundle's `setup
--no-pair`, a restart of the runner task, and a prune of all but the current and the
previous version.

`~/.canopy/hold-updates` holds the box on what it runs (a build being tested): a stale
answer is then `held` and nothing is installed — the same file `install-runner.sh
--if-stale` honours, so it means one thing whichever way the box was installed.

The last line printed is always `<status> <sha>`, like `update-check`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from . import bundle, platform_jobs, update
from .client import ClientError

logger = logging.getLogger("canopy_runner.self_update")

RELEASES_PATH = "/runner-releases/current"
#: Nothing we ship is near this; a bigger answer is not ours.
MAX_SIZE = 2 * 1024 ** 3
CHUNK = 1 << 20
DOWNLOAD_TIMEOUT = 60
WINDOWS_INSTALL_TIMEOUT = 15 * 60
NOT_BUNDLED = ("not a bundled install; developer installs update via install-runner.sh")
HELD = "held"


def hold_path(home: Path) -> Path:
    return home / ".canopy" / "hold-updates"


class UpdateError(Exception):
    """An install step failed; the running install is untouched (or restored)."""


# --- the decision ------------------------------------------------------------------


def current_release(client, platform: str) -> Optional[dict]:
    """The server's answer for this platform, or None when it is unknown (404, 503,
    network, nonsense)."""
    path = f"{RELEASES_PATH}?platform={urllib.parse.quote(platform)}"
    try:
        _, body = client._call("GET", path)
    except ClientError as exc:
        if exc.status == 404:
            logger.info("self-update: no release built for the deployed runner (%s)", platform)
        else:
            logger.warning("self-update: could not ask canopy-web: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — a timer must not crash on a bad answer
        logger.warning("self-update: could not ask canopy-web: %s", exc)
        return None
    if not isinstance(body, dict):
        return None
    sha = str(body.get("sha") or "").strip()
    if not sha or not body.get("url") or not body.get("sha256"):
        return None
    return body


def decide(cfg, client, *, installed: str, platform: str, now: Optional[float] = None
           ) -> tuple[str, Optional[dict]]:
    """(status, release) — update.update_status's rules against the release route."""
    rel = current_release(client, platform)
    if rel is None or not installed:
        return update.UNKNOWN, rel
    expected = str(rel["sha"]).strip()
    if expected == installed:
        return update.CURRENT, rel
    if update.in_flight(cfg, now=now):
        return update.BUSY, rel
    return update.STALE, rel


# --- download ----------------------------------------------------------------------


def download(rel: dict, dest_dir: Path, *, opener=urllib.request.urlopen) -> Path:
    """Stream the artifact to dest_dir/<file>, checking size and sha256 against `rel`.
    A mismatch deletes what was written and raises."""
    name = Path(str(rel.get("file") or "")).name
    if not name:
        raise UpdateError("the release names no file")
    try:
        size = int(rel.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if not 0 < size <= MAX_SIZE:
        raise UpdateError(f"the release's size ({rel.get('size')!r}) is not believable")
    want = str(rel["sha256"]).lower()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    got = 0
    try:
        # The URL is presigned: it carries its own authority, so no bearer goes with it.
        with opener(str(rel["url"]), timeout=DOWNLOAD_TIMEOUT) as resp, open(part, "wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > size:
                    raise UpdateError(f"the download is larger than the {size} bytes announced")
                h.update(chunk)
                fh.write(chunk)
        if got != size:
            raise UpdateError(f"the download is {got} bytes, not the {size} announced")
        if h.hexdigest() != want:
            raise UpdateError("the download's sha256 does not match the release — refusing it")
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def _release_sha(root: Path) -> str:
    try:
        return str(json.loads((root / bundle.RELEASE_FILE).read_text(encoding="utf-8"))
                   .get("sha") or "")
    except (OSError, ValueError, AttributeError):
        return ""


# --- macOS -------------------------------------------------------------------------


def team_id(app: Path, *, run=subprocess.run) -> str:
    """The signing Team ID of `app`, or "" when it is unsigned / ad-hoc."""
    p = run(["codesign", "-dv", "--verbose=2", str(app)], capture_output=True, text=True,
            timeout=60)
    for line in (p.stderr or "").splitlines() + (p.stdout or "").splitlines():
        if line.startswith("TeamIdentifier="):
            val = line.split("=", 1)[1].strip()
            return "" if val in ("", "not set") else val
    return ""


def install_macos(archive: Path, expected: str, app: Path, config: Path, *,
                  run=subprocess.run, out=print, env=None) -> None:
    parent = app.parent
    staging = parent / f".Canopy Runner.app.new-{expected[:12]}"
    old = parent / ".Canopy Runner.app.old"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        p = run(["ditto", "-x", "-k", str(archive), str(staging)], capture_output=True,
                text=True, timeout=600)
        if p.returncode != 0:
            raise UpdateError(f"could not unpack the update ({(p.stderr or '').strip()[:200]})")
        new_app = staging / bundle.APP_NAME
        if not new_app.is_dir():
            found = [c for c in staging.iterdir() if c.suffix == ".app"]
            if len(found) != 1:
                raise UpdateError("the update holds no Canopy Runner.app")
            new_app = found[0]
        p = run(["codesign", "--verify", "--deep", "--strict", str(new_app)],
                capture_output=True, text=True, timeout=300)
        if p.returncode != 0:
            raise UpdateError("the new app's code signature does not verify — refusing it "
                              f"({(p.stderr or '').strip()[:200]})")
        running_team = team_id(app, run=run)
        new_team = team_id(new_app, run=run)
        if running_team and new_team != running_team:
            raise UpdateError(f"the new app is signed by team {new_team or '(none)'}, not "
                              f"{running_team} like the running one — refusing it")
        if not running_team:
            logger.warning("self-update: the running app is not Developer ID signed — "
                           "accepting the update on its sha256 alone")
        runtime = new_app / "Contents" / "Resources" / "runtime"
        if _release_sha(runtime) != expected:
            raise UpdateError("the new app's release.json does not name the expected build")
        # The swap: two renames, rolled back between them.
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        os.rename(app, old)
        try:
            os.rename(new_app, app)
        except OSError:
            os.rename(old, app)
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)
    out(f"==> installed {expected[:12]} at {app}")

    # From here the new app is in place for good; a failure is reported, not undone
    # (the jobs point at version-free paths, so the next login picks it up regardless).
    new_bin = app / "Contents" / "Resources" / "runtime" / "bin" / "canopy-runner"
    child_env = {**(os.environ if env is None else env), "CANOPY_RUNNER_UNDER_JOB": "updater"}
    p = run([str(new_bin), "setup", "--no-pair", "--config", str(config)],
            capture_output=True, text=True, timeout=600, env=child_env)
    out((p.stdout or "").rstrip())
    if p.returncode != 0:
        out(f"WARNING: the new app's setup failed ({(p.stderr or '').strip()[-300:]})")
    # Relaunch the menu-bar app: quit the running copy by its executable path (another
    # account's copy is never matched), then open the new one without stealing focus.
    run(["pkill", "-f", str(app / "Contents" / "MacOS" / "CanopyRunner")],
        capture_output=True, timeout=30)
    run(["open", "-g", str(app)], capture_output=True, timeout=30)
    shutil.rmtree(old, ignore_errors=True)


# --- Windows -----------------------------------------------------------------------


def install_windows(setup_exe: Path, expected: str, app_dir: Path, config: Path, *,
                    run=subprocess.run, out=print, env=None) -> None:
    current = app_dir / "current"
    previous = Path(os.path.realpath(current))
    p = run([str(setup_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE"],
            capture_output=True, text=True, timeout=WINDOWS_INSTALL_TIMEOUT,
            **platform_jobs.no_window())
    if p.returncode != 0:
        raise UpdateError(f"the installer failed (exit {p.returncode})")
    if _release_sha(current) != expected:
        raise UpdateError("after the installer ran, current\\release.json does not name the "
                          "expected build")
    out(f"==> installed {expected[:12]} at {current}")
    py = current / "python" / "python.exe"
    child_env = {**(os.environ if env is None else env), "CANOPY_RUNNER_UNDER_JOB": "updater"}
    p = run([str(py), "-m", "canopy_runner.main", "setup", "--no-pair", "--config", str(config)],
            capture_output=True, text=True, timeout=600, env=child_env,
            **platform_jobs.no_window())
    out((p.stdout or "").rstrip())
    if p.returncode != 0:
        out(f"WARNING: the new install's setup failed ({(p.stderr or '').strip()[-300:]})")
    run(["schtasks", "/End", "/TN", platform_jobs.WINDOWS_RUNNER_TASK],
        capture_output=True, text=True, timeout=30, **platform_jobs.no_window())
    run(["schtasks", "/Run", "/TN", platform_jobs.WINDOWS_RUNNER_TASK],
        capture_output=True, text=True, timeout=30, **platform_jobs.no_window())
    prune_versions(app_dir, keep={Path(os.path.realpath(current)), previous})


def prune_versions(app_dir: Path, keep: set) -> list[str]:
    """Delete versions\\* except `keep`; a directory still in use is skipped."""
    removed = []
    keep_r = {Path(os.path.realpath(k)) for k in keep}
    vdir = app_dir / "versions"
    try:
        entries = sorted(vdir.iterdir())
    except OSError:
        return removed
    for d in entries:
        if not d.is_dir() or Path(os.path.realpath(d)) in keep_r:
            continue
        try:
            shutil.rmtree(d)
            removed.append(d.name)
        except OSError:
            pass  # locked (a process still runs from it) — next time
    return removed


# --- the command -------------------------------------------------------------------


def run_self_update(config_path: Path, *, if_stale: bool = False, platform: str = "",
                    executable: Optional[str] = None, home: Optional[Path] = None,
                    client_factory=None, installed: Optional[str] = None,
                    downloader: Callable[..., Path] = download, run=subprocess.run,
                    system: Optional[str] = None, out=print, now: Optional[float] = None) -> int:
    """Print `<status> <sha>` last. 0 unless an install was attempted and failed.

    `if_stale` is the timer's flag: install when stale. Without it the check runs and
    prints its answer but installs nothing (a dry look a person can run safely)."""
    root = bundle.root(executable)
    if root is None:
        out(NOT_BUNDLED)
        return 0
    from . import provenance
    from .config import Config
    from .runner_token import RunnerCredential

    home = home or Path.home()
    try:
        cfg = Config.load(Path(config_path))
    except (OSError, ValueError, TypeError) as exc:
        out(f"cannot read {config_path}: {exc}")
        out("unknown -")
        return 0
    if client_factory is None:
        from .client import Client

        client = Client(cfg.base_url, RunnerCredential.for_config(cfg))
    else:
        client = client_factory(cfg)
    plat = platform or bundle.platform(executable)
    inst = provenance.code_sha() if installed is None else installed
    status, rel = decide(cfg, client, installed=inst, platform=plat, now=now)
    sha = str((rel or {}).get("sha") or "-")
    expected = str((rel or {}).get("sha") or "")

    def record(status: str, detail: str = "") -> None:
        update.record(cfg, status, expected, installed=inst, detail=detail, now=now)

    if status == update.STALE and hold_path(home).exists():
        out(f"updates are held ({hold_path(home)}) — delete it to resume")
        status = HELD
    if status != update.STALE or not if_stale:
        # A dry look (no --if-stale) is a person's, not the updater's: what it
        # found must not stand in for the timer's last run.
        if if_stale:
            record(status, f"canopy-web named no {plat} build for the deployed runner"
                   if rel is None else "")
        out(f"{status} {sha}")
        return 0

    system = system or __import__("platform").system()
    try:
        artifact = downloader(rel, home / ".canopy" / "updates" / sha)
        if system == "Darwin":
            app = bundle.app_path(executable)
            if app is None:
                raise UpdateError(f"{root} is not inside Canopy Runner.app — reinstall it")
            install_macos(artifact, sha, app, Path(config_path), run=run, out=out)
        elif system == "Windows":
            app_dir = bundle.windows_app_dir(executable)
            if app_dir is None:
                raise UpdateError(f"{root} is not inside a Canopy Runner install")
            install_windows(artifact, sha, app_dir, Path(config_path), run=run, out=out)
        else:
            raise UpdateError(f"no installer for {system}")
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        record(update.FAILED, str(exc))
        out(f"update failed: {exc}")
        out(f"{status} {sha}")
        return 1
    finally:
        shutil.rmtree(home / ".canopy" / "updates" / sha, ignore_errors=True)
    # Installed: the new runner's own first check reports `current`; until then the
    # daemon says what was put in place.
    update.record(cfg, update.CURRENT, expected, installed=expected, now=now)
    out(f"{status} {sha}")
    return 0
