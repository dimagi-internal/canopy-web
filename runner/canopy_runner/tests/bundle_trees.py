"""Fake installer bundles on disk, for the bundle/setup/self-update tests."""
from __future__ import annotations

import json
from pathlib import Path

SHA = "a" * 40


def mac_app(base: Path, sha: str = SHA, *, platform: str = "macos-arm64") -> Path:
    """<base>/Applications/Canopy Runner.app with a runtime; returns the app."""
    app = base / "Applications" / "Canopy Runner.app"
    rt = app / "Contents" / "Resources" / "runtime"
    for d in ("python/bin", "node/bin", "acp/bin", "bin"):
        (rt / d).mkdir(parents=True, exist_ok=True)
    (rt / "release.json").write_text(json.dumps({"sha": sha, "platform": platform,
                                                 "built_at": "2026-10-19T12:00:00Z"}))
    (rt / "python" / "bin" / "python3").write_text("")
    for name in ("canopy-runner", "canopy-device-key"):
        (rt / "bin" / name).write_text("#!/bin/sh\n")
        (rt / "bin" / name).chmod(0o755)
    (app / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    return app


def mac_python(app: Path) -> str:
    return str(app / "Contents" / "Resources" / "runtime" / "python" / "bin" / "python3")


def windows_install(base: Path, sha: str = SHA) -> Path:
    """<base>/Canopy Runner/{versions/<sha12>, current → it}; returns the install dir."""
    app = base / "Canopy Runner"
    ver = app / "versions" / sha[:12]
    add_windows_version(app, sha)
    (app / "current").symlink_to(ver, target_is_directory=True)
    return app


def add_windows_version(app: Path, sha: str) -> Path:
    ver = app / "versions" / sha[:12]
    (ver / "python").mkdir(parents=True, exist_ok=True)
    (ver / "node").mkdir(exist_ok=True)
    (ver / "release.json").write_text(json.dumps({"sha": sha, "platform": "windows-x86_64"}))
    for exe in ("python.exe", "pythonw.exe"):
        (ver / "python" / exe).write_text("")
    return ver
