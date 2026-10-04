#!/usr/bin/env python3
"""Keep a cloud runner's disk from filling with caches nobody reads.

cloud-ec2-1's 20 GB root hit 100% on 2026-10-04. Most of it was
~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/: Claude Code copies
each plugin version it installs there and this box never removed one — 33
canopy and 24 ace versions, about 9 GB — while bootstrap updated plugins daily.
npm's and uv's download caches took most of the rest. It was cleaned by hand.

This runs from bootstrap_agents.sh (step 4, after the plugin updates, so the
version just installed is already recorded) — i.e. on every service start and
every daily self-refresh, when no turn is running:

  * plugin cache: per plugin, keep every version `installed_plugins.json` points
    at (any scope) plus the newest KEEP_NEWEST others by mtime — a margin for a
    session that started on the previous version — and delete the rest. If the
    install record cannot be read, delete NOTHING: guessing what is installed is
    how a prune deletes the plugin a turn is about to load.
  * npm: `npm cache clean --force` once ~/.npm/_cacache passes NPM_MAX_MB. It is
    a download cache; the installed tools (claude, tsx) live elsewhere.
  * uv: `uv cache prune`, which removes only entries nothing references.

Stdlib only. Never raises out of main(): a prune that fails must not fail a
bootstrap.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

KEEP_NEWEST = 2
NPM_MAX_MB = 500


def _log(msg: str) -> None:
    print(f"[prune-caches] {msg}", flush=True)


def dir_bytes(path: pathlib.Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def installed_paths(record: pathlib.Path) -> set[pathlib.Path] | None:
    """Every installPath in installed_plugins.json, resolved; None if unreadable
    (which the caller must treat as "prune nothing")."""
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    out: set[pathlib.Path] = set()
    for entries in plugins.values():
        for e in entries if isinstance(entries, list) else [entries]:
            p = (e or {}).get("installPath") if isinstance(e, dict) else None
            if p:
                out.add(pathlib.Path(p).resolve())
    return out


def plan_plugin_prune(cache: pathlib.Path, installed: set[pathlib.Path],
                      keep_newest: int = KEEP_NEWEST) -> list[pathlib.Path]:
    """The version directories to delete. Layout: cache/<mp>/<plugin>/<version>/."""
    doomed: list[pathlib.Path] = []
    if not cache.is_dir():
        return doomed
    for mp in sorted(p for p in cache.iterdir() if p.is_dir() and not p.is_symlink()):
        for plugin in sorted(p for p in mp.iterdir() if p.is_dir() and not p.is_symlink()):
            versions = [v for v in plugin.iterdir() if v.is_dir() and not v.is_symlink()]
            spare = [v for v in versions if v.resolve() not in installed]
            spare.sort(key=lambda v: v.stat().st_mtime, reverse=True)
            doomed.extend(spare[keep_newest:])
    return doomed


def prune_plugins(home: pathlib.Path, *, keep_newest: int, dry_run: bool) -> int:
    plugins = home / ".claude" / "plugins"
    installed = installed_paths(plugins / "installed_plugins.json")
    if installed is None:
        _log("installed_plugins.json unreadable: pruning NO plugin versions")
        return 0
    freed = 0
    for d in plan_plugin_prune(plugins / "cache", installed, keep_newest):
        size = dir_bytes(d)
        _log(f"{'would remove' if dry_run else 'removing'} {d} ({size / 2**20:.0f} MB)")
        if not dry_run:
            shutil.rmtree(d, ignore_errors=True)
        freed += size
    return freed


def prune_npm(home: pathlib.Path, *, max_mb: int, dry_run: bool) -> int:
    cache = home / ".npm" / "_cacache"
    if not cache.is_dir():
        return 0
    size = dir_bytes(cache)
    if size < max_mb * 2**20:
        return 0
    _log(f"npm cache {size / 2**20:.0f} MB > {max_mb} MB: cleaning")
    if dry_run or not shutil.which("npm"):
        return size if dry_run else 0
    subprocess.run(["npm", "cache", "clean", "--force"], capture_output=True, timeout=600)
    return max(0, size - dir_bytes(cache))


def prune_uv(*, dry_run: bool) -> None:
    if dry_run or not shutil.which("uv"):
        return
    subprocess.run(["uv", "cache", "prune"], capture_output=True, timeout=600)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="prune plugin / npm / uv caches")
    ap.add_argument("--home", default=str(pathlib.Path.home()))
    ap.add_argument("--keep-newest", type=int, default=KEEP_NEWEST)
    ap.add_argument("--npm-max-mb", type=int, default=NPM_MAX_MB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    home = pathlib.Path(args.home)
    freed = 0
    for step in (lambda: prune_plugins(home, keep_newest=args.keep_newest, dry_run=args.dry_run),
                 lambda: prune_npm(home, max_mb=args.npm_max_mb, dry_run=args.dry_run)):
        try:
            freed += step() or 0
        except Exception as exc:  # noqa: BLE001 — a prune must never fail bootstrap
            _log(f"a prune step failed ({exc}); continuing")
    try:
        prune_uv(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        _log(f"uv cache prune failed ({exc}); continuing")
    _log(f"freed ~{freed / 2**20:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
