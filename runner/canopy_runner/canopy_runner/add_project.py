"""Add a repo as an emdash project when canopy-web asks — the laptop half of the
runner page's "Add a repo" and the "Get it there…" on a laptop that lacks an
agent's repo (canopy-web `runner_projects`).

A laptop runs an agent in the emdash project named after it, and canopy routes no
work for that agent here until this box's heartbeat reports that project. Making
it used to be a person at this Mac cloning the repo and adding it in emdash's UI.

The ask rides the heartbeat reply (`add_projects`: id, name, repo_url). For each:

1. Already an emdash project by that name (emdash's DB): report added.
2. Clone it to `<repos root>/<name>` with this account's own git credentials, in
   the background (a clone can take minutes; the loop keeps claiming). A folder
   already there is used as is when it is a checkout of the same repo.
3. On a later tick, in the loop's own thread (it drives emdash's UI, as a turn's
   create does), open the checkout in emdash's Add Project dialog under `name`
   (`cdp_control.add_project`).
4. Check emdash's DB holds the project — a dialog that closed is not a project —
   and report the outcome with the reason.

Never writes emdash's DB. A failure marks only that ask failed on canopy-web; it
never marks this box not-ready (a missing repo is a setup gap, not box health).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import cdp_control, emdash
from .client import ClientError

logger = logging.getLogger(__name__)

#: Same rules canopy-web applies; checked again here because these become a folder
#: name and a `git clone` argument on this box.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
REPO_URL_RE = re.compile(r"^(https://|ssh://|git@)[^\s]+$")

CLONE_TIMEOUT = 900
#: How long emdash gets to show the project in its DB after the dialog closed.
VERIFY_SECONDS = 15.0
#: A dialog a person had open: wait this long before trying the UI step again.
BUSY_RETRY_SECONDS = 60.0

Run = Callable[..., subprocess.CompletedProcess]


@dataclass
class _Ask:
    key: str
    id: str
    name: str
    repo_url: str
    dest: Path
    clone: threading.Thread | None = None
    cloned: bool = False
    error: str = ""
    outcome: tuple[bool, str] | None = None
    not_before: float = 0.0


_lock = threading.Lock()
_asks: dict[str, _Ask] = {}
#: Asks answered (and reported) in this process, by id + requested_at: a re-armed
#: ask carries a new requested_at, so it is a new attempt.
_answered: set[str] = set()


def repos_root(home: Path | None = None) -> Path:
    """Where a new checkout goes: the folder these Macs already keep repos in —
    emdash's own default (`~/emdash/repositories`), else the older
    `~/emdash-projects`, else emdash's default."""
    home = home or Path.home()
    for candidate in (home / "emdash" / "repositories", home / "emdash-projects"):
        if candidate.is_dir():
            return candidate
    return home / "emdash" / "repositories"


def _norm_remote(url: str) -> str:
    """`https://github.com/O/R(.git)`, `git@github.com:O/R.git` and `ssh://git@github.com/O/R`
    all name one repo; compare them as `host/o/r`."""
    u = (url or "").strip().lower().rstrip("/")
    u = u[:-4] if u.endswith(".git") else u
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    return u.replace(":", "/", 1) if "/" in u and ":" in u.split("/", 1)[0] else u


def observe(me: dict | None, cfg, client, *, healthy: bool = True, run: Run = subprocess.run,
            add: Callable[..., dict] | None = None, list_projects: Callable[[str], list[str]] | None = None,
            root: Path | None = None, now: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep, background: bool = True) -> None:
    """Act on the heartbeat reply's `add_projects`. Cheap when there are none."""
    asks = me.get("add_projects") if isinstance(me, dict) else None
    if not isinstance(asks, list) or not asks:
        return
    add = add or cdp_control.add_project
    list_projects = list_projects or emdash.list_projects
    root = root or repos_root()
    ui_done = False
    for raw in asks:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        key = f"{raw['id']}:{raw.get('requested_at') or ''}"
        if key in _answered:
            continue
        with _lock:
            ask = _asks.get(key)
            if ask is None:
                name = str(raw.get("name") or "")
                ask = _Ask(key=key, id=str(raw["id"]), name=name, repo_url=str(raw.get("repo_url") or ""),
                           dest=root / name)
                _asks[key] = ask
        if ask.outcome is None:
            ask.outcome = _decide(ask, cfg, run=run, list_projects=list_projects, background=background)
        if ask.outcome is None and ask.cloned and healthy and not ui_done and now() >= ask.not_before:
            ui_done = True   # one emdash dialog per tick
            ask.outcome = _add_in_emdash(ask, cfg, add=add, list_projects=list_projects, sleep=sleep,
                                         now=now)
        if ask.outcome is not None:
            _report(ask, cfg, client)


def _decide(ask: _Ask, cfg, *, run: Run, list_projects, background: bool) -> tuple[bool, str] | None:
    """An outcome reached without touching emdash's UI, or None to keep going."""
    from . import desktop

    if not desktop.needs_emdash():
        return False, "this runner runs claude-cli, which does not use emdash projects"
    if not NAME_RE.match(ask.name):
        return False, f"{ask.name!r} cannot be an emdash project name"
    if not REPO_URL_RE.match(ask.repo_url):
        return False, f"{ask.repo_url!r} is not a repo URL this runner will clone"
    db = cfg.emdash_db or ""
    if not db or not Path(db).is_file():
        return False, "emdash's database is not on this runner (is emdash installed for this macOS account?)"
    try:
        if ask.name in list_projects(db):
            return True, f"'{ask.name}' is already an emdash project here"
    except emdash.EmdashReadError as exc:
        logger.warning("add project %s: cannot read emdash's projects yet: %s", ask.name, exc)
        return None
    if ask.error:
        return False, ask.error
    if ask.cloned:
        return None
    if ask.clone is None:
        if ask.dest.exists():
            ask.error = _existing_checkout_problem(ask, run)
            ask.cloned = not ask.error
            return (False, ask.error) if ask.error else None
        ask.clone = threading.Thread(target=_clone, args=(ask, run), daemon=True,
                                     name=f"canopy-clone-{ask.name}")
        logger.info("add project %s: cloning %s into %s", ask.name, ask.repo_url, ask.dest)
        if background:
            ask.clone.start()
        else:
            ask.clone.run()
    if ask.clone is not None and ask.clone.is_alive():
        return None
    if ask.error:
        return False, ask.error
    return None


def _git_env() -> dict:
    # Never wait on a credential prompt nobody can see: fail, and say so.
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
            "GCM_INTERACTIVE": "never"}


def _clone(ask: _Ask, run: Run) -> None:
    try:
        ask.dest.parent.mkdir(parents=True, exist_ok=True)
        proc = run(["git", "clone", "--", ask.repo_url, str(ask.dest)], capture_output=True, text=True,
                   timeout=CLONE_TIMEOUT, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        ask.error = f"git clone {ask.repo_url} failed: {exc}"
        return
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or "").strip()[-600:]
        ask.error = (f"git clone {ask.repo_url} failed on this runner (its macOS account's own git "
                     f"credentials): {said}")
        return
    ask.cloned = True


def _existing_checkout_problem(ask: _Ask, run: Run) -> str:
    """'' when the folder already there is a checkout of this repo; else why not."""
    try:
        proc = run(["git", "-C", str(ask.dest), "remote", "get-url", "origin"], capture_output=True,
                   text=True, timeout=30, env=_git_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{ask.dest} exists and could not be read as a git checkout: {exc}"
    if proc.returncode != 0:
        return (f"{ask.dest} already exists and is not a git checkout with an origin — move it "
                "aside, or add it in emdash yourself")
    origin = (proc.stdout or "").strip()
    if _norm_remote(origin) != _norm_remote(ask.repo_url):
        return (f"{ask.dest} already exists as a checkout of {origin}, not {ask.repo_url} — move it "
                "aside, or add it in emdash yourself")
    logger.info("add project %s: using the existing checkout at %s", ask.name, ask.dest)
    return ""


def _add_in_emdash(ask: _Ask, cfg, *, add, list_projects, sleep, now) -> tuple[bool, str] | None:
    try:
        add(ask.name, str(ask.dest), port=cfg.cdp_port)
    except cdp_control.CDPError as exc:
        msg = str(exc)
        if msg.startswith("BUSY"):
            ask.not_before = now() + BUSY_RETRY_SECONDS
            logger.info("add project %s: %s — trying again later", ask.name, msg)
            return None
        return False, f"emdash's Add Project dialog did not take {ask.dest}: {msg}"
    deadline = now() + VERIFY_SECONDS
    while True:
        try:
            if ask.name in list_projects(cfg.emdash_db):
                logger.info("add project %s: emdash holds it (%s)", ask.name, ask.dest)
                return True, f"cloned to {ask.dest} and added to emdash as '{ask.name}'"
        except emdash.EmdashReadError:
            pass
        if now() >= deadline:
            return False, (f"emdash's Add Project dialog accepted {ask.dest} but emdash's database has "
                           f"no project named '{ask.name}' after {VERIFY_SECONDS:.0f}s (the folder may "
                           "already be a project under another name)")
        sleep(0.5)


def _report(ask: _Ask, cfg, client) -> None:
    ok, detail = ask.outcome or (False, "")
    try:
        client.report_project_result(cfg.runner_id, ask.id, ok, detail)
    except ClientError as exc:
        if exc.status == 404:   # withdrawn meanwhile
            pass
        else:
            logger.warning("add project %s: could not report (%s); will retry", ask.name, exc)
            return
    (logger.info if ok else logger.warning)("add project %s: %s — %s", ask.name,
                                            "added" if ok else "failed", detail)
    with _lock:
        _answered.add(ask.key)
        _asks.pop(ask.key, None)


def _reset_for_tests() -> None:
    with _lock:
        _asks.clear()
        _answered.clear()
