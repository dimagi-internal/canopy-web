"""Stored agents on a laptop: an agent whose definition is kept in canopy-web, not in a
GitHub repo (canopy-web docs/architecture/runners.md § A stored agent on a runner).

A laptop runs an agent in a checkout of its repo — an emdash project, or a desktop
worktree. A stored agent has no repo; its checkout is `~/.canopy/agents/<slug>`, the same
directory `canopy agent use <slug>` materializes for its person. Before a turn runs, the
runner brings that directory to the live revision (`ensure_checkout`), so a turn never
starts on whatever revision happened to be there.

The claim says which turns these are (`turn["definition"]["source"] == "stored"`).

On a laptop with emdash, that directory is ALSO the agent's emdash project, named after
its slug (`ensure_emdash_project`), added through emdash's own Add Project dialog the
first time a turn needs it. emdash then drives the agent's turns, confined ones included,
exactly as it drives a repo agent's: the same worktrees, the same caller pointer and
native permission rules. Only a laptop without emdash runs a stored agent headless
(`desktop.maybe_execute`), and a caller's confined turn never runs headless.

The CLI call is `canopy agent materialize --slug <slug> --dest <dir>`, which writes the
live revision when the directory is behind and does nothing when it is current — the
same as `canopy agent sync`, with the directory named rather than inferred. A CLI that
predates stored agents fails the turn, saying to update canopy: running the last
materialized revision without a word is exactly what must not happen.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import cdp_control, emdash
from .device_key import canopy_cli
from .platform_jobs import no_window

logger = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]

TIMEOUT = 300
UPDATE_HINT = "update canopy on this computer (`/canopy:update` in Claude Code, then restart the runner)"
#: Reported on every heartbeat as `stored_projects`: this code makes a stored agent's
#: checkout its emdash project, so canopy-web hands this laptop the agent's confined turns.
STORED_PROJECTS_VERSION = 1
#: How long emdash gets to show the project in its DB after its dialog closed.
VERIFY_SECONDS = 15.0


class StoredAgentError(RuntimeError):
    """A stored agent's turn that must not run: its checkout could not be brought to the
    live revision."""


def is_stored(turn: dict | None) -> bool:
    d = (turn or {}).get("definition")
    return isinstance(d, dict) and d.get("source") == "stored"


def agent_dir(slug: str, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".canopy" / "agents" / slug


def _too_old(returncode: int, said: str) -> bool:
    return returncode == 2 or any(m in said for m in ("No such command", "No such option",
                                                       "Missing option"))


def ensure_checkout(slug: str, *, run: Run = subprocess.run, home: Path | None = None) -> Path:
    """`~/.canopy/agents/<slug>` at the agent's live revision. Raises StoredAgentError."""
    dest = agent_dir(slug, home)
    cli = canopy_cli(home)
    if cli is None:
        raise StoredAgentError(f"{slug} is a stored agent and this computer has no canopy CLI to "
                               f"fetch it with; {UPDATE_HINT}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = run([cli, "agent", "materialize", "--slug", slug, "--dest", str(dest)],
                capture_output=True, text=True, timeout=TIMEOUT, **no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        raise StoredAgentError(f"could not fetch stored agent {slug}: {exc}") from exc
    said = " ".join((r.stderr or r.stdout or f"exit {r.returncode}").split())[-300:]
    if r.returncode != 0:
        if _too_old(r.returncode, said):
            raise StoredAgentError(f"{slug} is a stored agent, and this computer's canopy CLI cannot "
                                   f"fetch one ({said}); {UPDATE_HINT}")
        raise StoredAgentError(f"could not fetch stored agent {slug}: {said}")
    doc = _last_json(r.stdout or "")
    if doc is None or not (dest / ".git").exists():
        raise StoredAgentError(f"fetching stored agent {slug} left no checkout at {dest} ({said})")
    if doc.get("changed"):
        logger.info("stored agent %s: now at revision %s", slug, doc.get("revision"))
        refresh_plugin(slug, dest, run=run)
    return dest


def _last_json(out: str) -> dict | None:
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and "revision" in doc:
                return doc
    return None


def refresh_plugin(slug: str, dest: Path, *, run: Run = subprocess.run) -> None:
    """Best-effort: bring the person's install of the agent's plugin to the revision just
    written, when it is installed from this directory (`canopy agent use`). Turns load
    the installed plugin, not the checkout. A failure is logged; the turn still runs."""
    from .desktop import _cli_env, claude_cli  # noqa: PLC0415 — desktop imports this module

    try:
        name = json.loads((dest / ".claude-plugin" / "marketplace.json").read_text()).get("name") or ""
    except (OSError, ValueError, AttributeError):
        return
    claude = claude_cli()
    if not name or claude is None:
        return
    env = _cli_env(claude)
    for args in (("marketplace", "update", name), ("update", f"{slug}@{name}", "--scope", "user")):
        try:
            out = run([str(claude), "plugin", *args], capture_output=True, text=True, timeout=TIMEOUT,
                      env=env, **no_window())
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("stored agent %s: claude plugin %s: %s", slug, " ".join(args), exc)
            return
        if out.returncode != 0:
            logger.warning("stored agent %s: claude plugin %s: %s", slug, " ".join(args),
                           (out.stderr or out.stdout or "")[-300:])
            return


def emdash_present(cfg) -> bool:
    """Whether this box has emdash (its database is here)."""
    db = getattr(cfg, "emdash_db", "") or ""
    return bool(db) and Path(db).is_file()


def ensure_emdash_project(cfg, slug: str, *, home: Path | None = None, ensure=None, add=None,
                          project_path=None, sleep: Callable[[float], None] = time.sleep,
                          now: Callable[[], float] = time.monotonic) -> str:
    """Bring `~/.canopy/agents/<slug>` to the live revision and make it the emdash project
    `<slug>`. "" once emdash holds that project at that checkout, else a note to fail the
    turn with. Idempotent: a project already there is checked, never added again.

    Added through emdash's Add Project dialog (`cdp_control.add_project`, the one
    `add_project.py` uses), never by writing emdash's DB, and checked in that DB
    afterwards: a dialog that closed is not a project. A project of that name at another
    folder is refused rather than driven: its turns would run a checkout that is not the
    agent's live revision."""
    ensure = ensure or ensure_checkout
    add = add or cdp_control.add_project
    project_path = project_path or emdash.project_path
    try:
        dest = ensure(slug, home=home) if home is not None else ensure(slug)
    except StoredAgentError as exc:
        return str(exc)
    db = cfg.emdash_db

    def held() -> str | None:
        return project_path(db, slug)

    try:
        have = held()
    except emdash.EmdashReadError as exc:
        return f"could not read emdash's projects to set up stored agent {slug}: {exc}"
    if have is not None:
        if _same_dir(have, dest):
            return ""
        return (f"emdash's project '{slug}' is {have}, not the stored agent's checkout {dest} — "
                f"remove that project from emdash, and the runner adds the checkout on the next turn")
    try:
        add(slug, str(dest), port=cfg.cdp_port)
    except cdp_control.CDPError as exc:
        msg = str(exc)
        if msg.startswith("BUSY"):
            return (f"emdash has a dialog open, so the runner could not add stored agent {slug}'s "
                    "checkout as a project; close it and send the message again")
        return f"emdash's Add Project dialog did not take {dest} for stored agent {slug}: {msg}"
    deadline = now() + VERIFY_SECONDS
    while True:
        try:
            have = held()
        except emdash.EmdashReadError:
            have = None
        if have is not None and _same_dir(have, dest):
            logger.info("stored agent %s: emdash project at %s", slug, dest)
            return ""
        if now() >= deadline:
            return (f"emdash's Add Project dialog accepted {dest}, but emdash has no project '{slug}' "
                    f"there after {VERIFY_SECONDS:.0f}s (the folder may be a project under another name)")
        sleep(0.5)


def _same_dir(a: str, b: Path) -> bool:
    try:
        return Path(a).expanduser().resolve() == b.expanduser().resolve()
    except OSError:
        return Path(a) == b
