"""Claude Code's workspace trust, as it applies to a caller's (`cx-`) session's own rules.

**What Claude Code checks (measured on 2.1.332).** Two different trust checks:

* a lenient one: is this folder, or the repository it belongs to, recorded as trusted
  (`projects[<path>].hasTrustDialogAccepted` in `~/.claude.json`). emdash writes that key
  for every worktree it creates, so a fleet session never sees the plain trust dialog;
* a strict one, for the grants a folder's own settings make: `permissions.allow` and
  `additionalDirectories` in `.claude/settings.json` apply only when the key is set for
  the folder's **repository root**. For a git worktree that root is the MAIN checkout
  (the directory holding the common `.git`), never the worktree itself. With only the
  worktree trusted, Claude Code drops the allow rules (`Dropped N project-scoped
  permissions.allow entries — workspace not yet trusted`) and, in an interactive start,
  stops on the "Accessing workspace … This folder pre-approves N tool permissions"
  backstop dialog, whose DEFAULT is "No, continue without these permissions".

So the confined profile's allow rules (native_permissions) either land with the main
checkout trusted, or are dropped after a dialog nobody is watching. Settings passed with
`--settings` (the cloud runner's `cli_settings`) are not project settings and are never
gated; this is the laptop (emdash) path only.

**What canopy does about it.**

* A STORED agent's checkout (`~/.canopy/agents/<slug>`, cloned and kept at the live
  revision by `stored_agents`) is a folder canopy itself writes. Before a confined
  session is created in it, canopy records the trust key for it (`trust_stored_checkout`)
  so the session's own rules apply and no dialog appears. Only that root qualifies: a
  person's own checkout is their trust decision, not canopy's.
* Anywhere else, if the main checkout is not trusted, the native layer is written
  WITHOUT allow rules (`allow_rules_apply`). They would be dropped anyway; writing them
  only adds the dialog. The deny list, which is the layer that holds under emdash's
  bypass mode, needs no trust and is written either way.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess

from . import stored_agents

logger = logging.getLogger(__name__)


def config_path() -> pathlib.Path:
    """Claude Code's state file: `$CLAUDE_CONFIG_DIR/.claude.json`, else `~/.claude.json`."""
    root = os.environ.get("CLAUDE_CONFIG_DIR")
    return pathlib.Path(root) / ".claude.json" if root else pathlib.Path.home() / ".claude.json"


def _load(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None  # unreadable or corrupt: never rewrite it


def main_checkout(path: str) -> str | None:
    """The repository root Claude Code keys strict trust on for `path`: the main
    checkout when `path` is in a git worktree, the repo's top level otherwise. None
    when `path` is not in a git repository or git cannot say."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    common = pathlib.Path(out)
    if common.name != ".git":
        return None  # a bare or separated git dir: no checkout to name
    return str(common.parent)


def is_trusted(path: str, *, config: pathlib.Path | None = None) -> bool:
    doc = _load(config or config_path())
    projects = doc.get("projects") if isinstance(doc, dict) else None
    entry = projects.get(path) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


def allow_rules_apply(worktree: str, *, config: pathlib.Path | None = None) -> bool:
    """Whether Claude Code will honour `permissions.allow` in `worktree`'s
    `.claude/settings.json`: its main checkout is recorded as trusted."""
    root = main_checkout(worktree)
    return root is not None and is_trusted(root, config=config)


def is_stored_checkout(path: str, *, root: pathlib.Path | None = None) -> bool:
    """`path` is a main checkout directly under canopy's stored-agents root, after
    resolving links."""
    base = root or stored_agents.agent_dir("_").parent
    try:
        real, real_base = pathlib.Path(path).resolve(), base.resolve()
    except OSError:
        return False
    # A main checkout holds a `.git` DIRECTORY; a worktree's `.git` is a file.
    return real.parent == real_base and (real / ".git").is_dir()


def trust_stored_checkout(path: str, *, config: pathlib.Path | None = None,
                          root: pathlib.Path | None = None) -> bool:
    """Record Claude Code trust for stored agent checkout `path` (and nothing else).

    Returns True when the key is set afterwards. Refuses (False) any path outside
    canopy's stored-agents root, and never rewrites a config file it cannot parse.
    The write is a read-modify-replace of the whole file, the same as emdash's own
    trust write; it happens only when the key is missing, which is once per agent."""
    if not path or not is_stored_checkout(path, root=root):
        return False
    key = str(pathlib.Path(path).resolve())
    cfg = config or config_path()
    doc = _load(cfg)
    if not isinstance(doc, dict):
        logger.warning("claude trust: %s unreadable; not recording trust for %s", cfg, key)
        return False
    projects = doc.get("projects") if isinstance(doc.get("projects"), dict) else {}
    entry = projects.get(key) if isinstance(projects.get(key), dict) else {}
    if entry.get("hasTrustDialogAccepted") is True:
        return True
    doc["projects"] = {**projects, key: {**entry, "hasTrustDialogAccepted": True}}
    tmp = cfg.with_name(cfg.name + ".canopy-trust-tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2))
        if cfg.exists():
            os.chmod(tmp, cfg.stat().st_mode & 0o777)
        os.replace(tmp, cfg)
    except OSError as exc:
        logger.warning("claude trust: could not record trust for %s: %s", key, exc)
        return False
    logger.info("claude trust: recorded trust for stored agent checkout %s", key)
    return True
