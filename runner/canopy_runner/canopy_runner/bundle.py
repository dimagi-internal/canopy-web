"""Is this runner running from a downloaded installer's bundle, and where is it?

The installers (macOS `Canopy Runner.app`, the Windows Setup) carry their own Python,
Node.js, ACP adapter and — on macOS — the device-key helper, so a person with no
developer tools can run a runner. Everything the runner would otherwise find on the
developer's PATH (`node`, `npm`, `claude-agent-acp`) is inside that bundle instead, and
the commands that manage the install (`setup`, `self-update`) have to know where it is.

The bundle is found from the interpreter, never from a setting: `sys.executable` is
`<root>/python/bin/python3` on macOS and `<root>\\python\\python[w].exe` on Windows, and
`<root>` is the directory holding `release.json`. Nothing about a developer install looks
like that, so `root()` is None there and every caller keeps its old behaviour.

`sys.executable` is made absolute but NOT resolved: on Windows the tasks run
`…\\Canopy Runner\\current\\python\\pythonw.exe` through the `current` junction, and
resolving it would name the versioned directory a later update deletes.

    macOS    ~/Applications/Canopy Runner.app/Contents/Resources/runtime/
    Windows  %LOCALAPPDATA%\\Programs\\Canopy Runner\\current\\ (junction → versions\\<sha12>)
"""
from __future__ import annotations

import json
import os
import platform as _platform
import sys
from pathlib import Path
from typing import Optional

RELEASE_FILE = "release.json"
APP_NAME = "Canopy Runner.app"


def _exe(executable: Optional[str]) -> Path:
    return Path(os.path.abspath(executable or sys.executable))


def root(executable: Optional[str] = None) -> Optional[Path]:
    """The bundle directory (the one holding release.json above python/), or None."""
    exe = _exe(executable)
    for anc in list(exe.parents)[:4]:
        if (anc / RELEASE_FILE).is_file() and (anc / "python").is_dir():
            return anc
    return None


def is_bundled(executable: Optional[str] = None) -> bool:
    return root(executable) is not None


def info(executable: Optional[str] = None) -> dict:
    """release.json — {"sha", "platform", "built_at"} — or {} when not bundled."""
    r = root(executable)
    if r is None:
        return {}
    try:
        data = json.loads((r / RELEASE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _windows_layout(r: Path) -> bool:
    return (r / "python" / "python.exe").exists() or (r / "python" / "pythonw.exe").exists()


def node_dir(executable: Optional[str] = None) -> Optional[Path]:
    """The directory holding the bundle's `node` (and `npm`)."""
    r = root(executable)
    if r is None:
        return None
    return r / "node" if _windows_layout(r) else r / "node" / "bin"


def acp_prefix(executable: Optional[str] = None) -> Optional[Path]:
    """The npm prefix the bundle's ACP adapter is installed under."""
    r = root(executable)
    return None if r is None else r / "acp"


def app_path(executable: Optional[str] = None) -> Optional[Path]:
    """macOS: the enclosing `Canopy Runner.app` when the bundle lives in one, else None."""
    r = root(executable)
    if r is None:
        return None
    parts = r.parents
    if (r.name == "runtime" and len(parts) >= 3 and parts[0].name == "Resources"
            and parts[1].name == "Contents" and parts[2].suffix == ".app"):
        return parts[2]
    return None


def windows_app_dir(executable: Optional[str] = None) -> Optional[Path]:
    """Windows: the install directory holding `versions\\` and `current` — the parent of
    the bundle (`current` itself, or `versions\\<sha12>` when run by that path)."""
    r = root(executable)
    if r is None:
        return None
    if r.name.lower() == "current":
        return r.parent
    if r.parent.name.lower() == "versions":
        return r.parent.parent
    return None


def host_platform(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    """The release platform name for THIS machine: macos-arm64, macos-x86_64,
    windows-x86_64, or linux-<arch> (never built; it only names the box)."""
    system = system or _platform.system()
    machine = (machine or _platform.machine() or "").lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}.get(
        machine, machine or "unknown")
    if system == "Darwin":
        return f"macos-{arch}"
    if system == "Windows":
        return f"windows-{arch}"
    return f"{system.lower()}-{arch}"


def platform(executable: Optional[str] = None) -> str:
    """Which release this bundle is: release.json's `platform`, else the stamped
    `_build_info.PLATFORM`, else this machine's."""
    named = str(info(executable).get("platform") or "")
    if named:
        return named
    from . import _build_info

    return getattr(_build_info, "PLATFORM", "") or host_platform()


def activate(executable: Optional[str] = None, *, env=None) -> Optional[Path]:
    """When bundled, put the bundle's node (and its ACP adapter's bin) first on PATH — so
    the CDP sidecar's `node`, `npm` for its deps and canopy_acp's `_node()` find it, as
    the macOS launch shim already arranges and the Windows tasks (which run pythonw.exe
    directly) do not — and return the root. A no-op (None) for a developer install."""
    r = root(executable)
    if r is None:
        return None
    env = os.environ if env is None else env
    acp = r / "acp" if _windows_layout(r) else r / "acp" / "bin"
    parts = [p for p in (env.get("PATH") or "").split(os.pathsep) if p]
    first = [str(d) for d in (node_dir(executable), acp) if d is not None]
    env["PATH"] = os.pathsep.join([*first, *(p for p in parts if p not in first)])
    return r


def adapter_prefixes(executable: Optional[str] = None) -> Optional[list]:
    """The ACP adapter search prefixes with the bundle's first, or None (the default
    search) for a developer install."""
    prefix = acp_prefix(executable)
    if prefix is None:
        return None
    from canopy_acp.client import ADAPTER_PREFIXES

    return [prefix, *ADAPTER_PREFIXES]
