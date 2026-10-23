"""What else is installed beside the canopy CLI, so reinstalling it does not drop it.

`uv tool install --reinstall --force <clone>` installs exactly what it is told: any package
someone put beside canopy with `--with` (the knowledge-base engine `canopy-kb`, a reader
like `openpyxl`) is silently gone after it. The runner reinstalls the CLI whenever it falls
behind the plugin, so a box that was set up to build knowledge bases lost the engine on the
next canopy release, and `canopy kb build` failed there days later (2026-10-23, cloud-ec2-2,
the GiveWell republish of canopy-web#1891). uv's own receipt records every requirement the
tool was installed with; this turns all but canopy itself back into `--with` arguments.

Stdlib only, and runnable as a script — the cloud bootstrap (runner/ec2/bootstrap_agents.sh)
calls it with `python3` and gets one `--with` argument per line. Fail-open: no receipt, an
unreadable one, a requirement it cannot spell — each yields fewer arguments, never an error.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit, urlunsplit

TOOL = "canopy"


def receipt_path(home: Optional[Path] = None, env: Optional[dict] = None) -> Path:
    """Where uv keeps the canopy tool's receipt: $UV_TOOL_DIR, else uv's data dir."""
    env = os.environ if env is None else env
    home = home or Path.home()
    if env.get("UV_TOOL_DIR"):
        root = Path(env["UV_TOOL_DIR"])
    elif os.name == "nt":
        root = Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "uv" / "tools"
    else:
        root = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "uv" / "tools"
    return root / TOOL / "uv-receipt.toml"


def _git_spec(name: str, url: str) -> str:
    """A receipt's `git = "https://…?subdirectory=x&rev=y"` as a PEP 508 direct reference."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    path = parts.path
    for key in ("rev", "tag", "branch"):
        if query.get(key):
            path = f"{path}@{query[key][0]}"
            break
    fragment = f"subdirectory={query['subdirectory'][0]}" if query.get("subdirectory") else ""
    return f"{name} @ git+{urlunsplit((parts.scheme, parts.netloc, path, '', fragment))}"


def spec(req: dict) -> str:
    """One receipt requirement as a `--with` value, or "" when it cannot be spelled."""
    name = str(req.get("name") or "").strip()
    if not name:
        return ""
    extras = req.get("extras") or []
    named = f"{name}[{','.join(extras)}]" if extras else name
    if req.get("git"):
        return _git_spec(named, str(req["git"]))
    if req.get("directory"):
        return str(req["directory"])
    if req.get("url"):
        return f"{named} @ {req['url']}"
    if req.get("path"):
        return str(req["path"])
    return f"{named}{req.get('specifier') or ''}"


def kept(home: Optional[Path] = None, env: Optional[dict] = None) -> list[str]:
    """The `--with` values canopy was installed with (canopy itself excluded)."""
    try:
        doc = tomllib.loads(receipt_path(home, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for req in (doc.get("tool") or {}).get("requirements") or []:
        if isinstance(req, dict) and str(req.get("name") or "").lower() != TOOL:
            value = spec(req)
            if value and value not in out:
                out.append(value)
    return out


def with_args(home: Optional[Path] = None, env: Optional[dict] = None) -> list[str]:
    """`["--with", v, …]` for every requirement to keep across a reinstall."""
    return [arg for value in kept(home, env) for arg in ("--with", value)]


if __name__ == "__main__":
    # One value per line, for `while read` in a shell: the bootstrap adds `--with` itself.
    sys.stdout.write("".join(f"{v}\n" for v in kept()))
