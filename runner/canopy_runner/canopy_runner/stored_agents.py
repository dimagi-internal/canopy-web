"""Stored agents on a laptop: an agent whose definition is kept in canopy-web, not in a
GitHub repo (canopy-web docs/architecture/runners.md § A stored agent on a runner).

A laptop runs an agent in a checkout of its repo — an emdash project, or a desktop
worktree. A stored agent has no repo; its checkout is `~/.canopy/agents/<slug>`, the same
directory `canopy agent use <slug>` materializes for its person. Before a turn runs, the
runner brings that directory to the live revision (`ensure_checkout`), so a turn never
starts on whatever revision happened to be there.

The claim says which turns these are (`turn["definition"]["source"] == "stored"`).

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
from collections.abc import Callable
from pathlib import Path

from .device_key import canopy_cli
from .platform_jobs import no_window

logger = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]

TIMEOUT = 300
UPDATE_HINT = "update canopy on this computer (`/canopy:update` in Claude Code, then restart the runner)"


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
