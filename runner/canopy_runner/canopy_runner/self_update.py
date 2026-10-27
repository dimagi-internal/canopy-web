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

Windows checks the downloaded Setup's Authenticode signature (Valid, signed by
WINDOWS_PUBLISHER) when the release says it is signed, then runs it with `/VERYSILENT …
/UPDATE`, which writes `versions\\<sha12>` and flips the `current` junction — and any
failure puts `current` back on the previous version. Then the new bundle's `setup
--no-pair`, a restart of the runner task that waits for /End to settle and confirms /Run
took (a runner that never comes back is a FAILED update), and a prune of all but the
current and the previous version.

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
import re
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


#: Who signs the Windows installer (Azure Trusted Signing, runner-release.yml). A
#: signature that Windows calls Valid but names someone else is a different publisher,
#: whatever the sha256 says — the Windows twin of the macOS Team ID check.
WINDOWS_PUBLISHER = "Dimagi"


def _subject_names(subject: str) -> list[str]:
    """The CN= and O= values of an X.500 subject (quoted values may hold commas)."""
    return [q or b for _, q, b in
            re.findall(r'(?:^|,)\s*(CN|O)=(?:"([^"]*)"|([^,]*))', subject or "")]


def authenticode(path: Path, *, run=subprocess.run) -> tuple[str, str]:
    """(status, signer subject) of `path` per Windows' own Get-AuthenticodeSignature."""
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath {_ps_quote(str(path))}; "
              "Write-Output ([string]$s.Status); Write-Output ([string]$s.SignerCertificate.Subject)")
    p = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script], capture_output=True, text=True, timeout=120,
            **platform_jobs.no_window())
    lines = [ln.strip() for ln in (p.stdout or "").splitlines()]
    if p.returncode != 0 or not lines:
        raise UpdateError("could not read the installer's signature "
                          f"({(p.stderr or '').strip()[:200]})")
    return lines[0], (lines[1] if len(lines) > 1 else "")


def verify_windows_signature(setup_exe: Path, *, signed: bool, run=subprocess.run,
                             out=print) -> None:
    """When the release says its build is signed (manifest `signed`, set by CI only for
    a signature Windows calls Valid), the download must be Valid AND signed by
    WINDOWS_PUBLISHER. An unsigned release is accepted on its sha256 alone, loudly."""
    if not signed:
        logger.warning("self-update: the release is not Authenticode signed — accepting the "
                       "installer on its sha256 alone")
        out("==> the release is unsigned: accepting the installer on its sha256 alone")
        return
    status, subject = authenticode(setup_exe, run=run)
    if status != "Valid":
        raise UpdateError(f"the installer's Authenticode signature is {status or 'missing'}, "
                          "not Valid, though the release says it is signed — refusing it")
    names = _subject_names(subject)
    if not any(re.match(rf"{re.escape(WINDOWS_PUBLISHER)}(?![\w-])", n.strip(), re.I)
               for n in names):
        raise UpdateError(f"the installer is signed by {subject or '(nobody)'}, not "
                          f"{WINDOWS_PUBLISHER} — refusing it")


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _relink(current: Path, target: Path, *, run=subprocess.run) -> None:
    """Point the `current` junction at `target` (rmdir on a junction drops the link,
    never what it points at)."""
    kw = dict(capture_output=True, text=True, timeout=30, **platform_jobs.no_window())
    if os.path.lexists(current):
        run(["cmd", "/c", "rmdir", str(current)], **kw)
    p = run(["cmd", "/c", "mklink", "/J", str(current), str(target)], **kw)
    if p.returncode != 0:
        raise OSError(f"mklink /J failed ({(p.stderr or p.stdout or '').strip()[:200]})")


def _restore_current(current: Path, previous: Path | None, *, run=subprocess.run,
                     out=print) -> None:
    """After a failed install: put `current` back on the version it named before, so
    both tasks (which run through `current`) still have something to run."""
    if previous is None or not previous.is_dir():
        return
    try:
        if os.path.lexists(current) and Path(os.path.realpath(current)) == previous:
            return
        _relink(current, previous, run=run)
        out(f"==> restored {current} -> {previous.name}")
    except (OSError, subprocess.SubprocessError) as exc:
        out(f"WARNING: could not point {current} back at {previous.name} ({exc}) — "
            "reinstall Canopy Runner")


def _run_installer(setup_exe: Path, *, popen=subprocess.Popen, run=subprocess.run,
                   timeout: float = WINDOWS_INSTALL_TIMEOUT) -> int:
    """Run the Inno Setup installer silently. On a timeout, kill the whole tree: the
    exe we start is only Inno's launcher, which runs the real setup as a child — a
    plain kill would leave that child writing the install."""
    proc = popen([str(setup_exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, **platform_jobs.no_window())
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, text=True,
            timeout=30, **platform_jobs.no_window())
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        raise UpdateError(f"the installer did not finish in {int(timeout)}s — stopped it")


def install_windows(setup_exe: Path, expected: str, app_dir: Path, config: Path, *,
                    run=subprocess.run, popen=subprocess.Popen, sleep=None,
                    signed: bool = False, out=print, env=None) -> None:
    verify_windows_signature(setup_exe, signed=signed, run=run, out=out)
    current = app_dir / "current"
    previous = Path(os.path.realpath(current)) if os.path.lexists(current) else None
    try:
        rc = _run_installer(setup_exe, popen=popen, run=run)
        if rc != 0:
            raise UpdateError(f"the installer failed (exit {rc})")
        if _release_sha(current) != expected:
            raise UpdateError("after the installer ran, current\\release.json does not name "
                              "the expected build")
    except BaseException:
        # Whatever the installer left (a removed junction, one pointing at a half
        # install), put `current` back where it was: both tasks run through it.
        _restore_current(current, previous, run=run, out=out)
        raise
    out(f"==> installed {expected[:12]} at {current}")
    py = current / "python" / "python.exe"
    child_env = {**(os.environ if env is None else env), "CANOPY_RUNNER_UNDER_JOB": "updater"}
    p = run([str(py), "-m", "canopy_runner.main", "setup", "--no-pair", "--config", str(config)],
            capture_output=True, text=True, timeout=600, env=child_env,
            **platform_jobs.no_window())
    out((p.stdout or "").rstrip())
    if p.returncode != 0:
        out(f"WARNING: the new install's setup failed ({(p.stderr or '').strip()[-300:]})")
    up = platform_jobs.restart_windows_task(platform_jobs.WINDOWS_RUNNER_TASK, run=run,
                                            sleep=sleep)
    # The .iss prunes too (PruneVersions, keeping the new version and the one `current`
    # named before); this pass is the updater's own, with the same keep set.
    prune_versions(app_dir, keep={Path(os.path.realpath(current)),
                                  *([previous] if previous else [])})
    if not up:
        raise UpdateError(f"installed {expected[:12]}, but {platform_jobs.WINDOWS_RUNNER_TASK} "
                          f"is not Running after a restart (waited "
                          f"{platform_jobs.TASK_SETTLE_SECONDS}s) — its 5-minute repeat "
                          "will try again")


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
            install_windows(artifact, sha, app_dir, Path(config_path), run=run, out=out,
                            signed=bool(rel.get("signed")))
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
