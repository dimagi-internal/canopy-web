"""What a local app keeps on this computer, under ~/.canopy/apps/<key>/.

Keys and versions come from a server's answer, so each is checked before it names a
path. Whether a supervisor is alive is answered by a file lock it holds, never by a pid:
a pid can be reused, and acting on a stale one would signal someone else's process.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from .manifest import KEY_RX, VERSION_RX

ENV_PASSED_TO_APPS = ("PATH", "HOME", "LANG", "TMPDIR")
DEFAULT_PATH = "/usr/bin:/bin"
HOST_DIED = "host_died"


def apps_root() -> Path:
    return Path.home() / ".canopy" / "apps"


def app_dir(key: str) -> Path:
    if not isinstance(key, str) or not KEY_RX.match(key):
        raise ValueError(f"not an app key: {key!r}")
    return apps_root() / key


def version_dir(key: str, version: str) -> Path:
    if not isinstance(version, str) or not VERSION_RX.match(version):
        raise ValueError(f"not a version: {version!r}")
    return app_dir(key) / "versions" / version


def run_file(key: str) -> Path:
    return app_dir(key) / "run.json"


def log_file(key: str) -> Path:
    return app_dir(key) / "log.jsonl"


def dev_file(key: str) -> Path:
    return app_dir(key) / "dev.json"


def would_send_file(key: str) -> Path:
    return app_dir(key) / "would-send.jsonl"


def spec_file(key: str) -> Path:
    return app_dir(key) / "start.json"


def lock_file(key: str) -> Path:
    return app_dir(key) / "host.lock"


def ensure_dir(key: str) -> Path:
    path = app_dir(key)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def scrubbed_env(source=None) -> dict:
    """The only environment an app or a check gets, so no token is handed over by accident."""
    source = os.environ if source is None else source
    env = {name: source[name] for name in ENV_PASSED_TO_APPS if source.get(name)}
    env.setdefault("PATH", DEFAULT_PATH)
    return env


def write_json(path: Path, value: dict) -> None:
    """Atomic and private, because readers poll this file while it changes."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def append_private_line(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as out:
        out.write(json.dumps(value) + "\n")


class HostLock:
    """Held by a supervisor for as long as it is responsible for an app's processes."""

    def __init__(self, key: str):
        self.key = key
        self._fd = None

    def acquire(self, wait: float = 1.0) -> bool:
        ensure_dir(self.key)
        fd = os.open(lock_file(self.key), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                # A `status` probing the lock holds it for an instant; only a lock
                # still held after this wait is another supervisor.
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def host_alive(key: str) -> bool:
    try:
        path = lock_file(key)
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
    except (OSError, ValueError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def installed_keys() -> list[str]:
    root = apps_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and KEY_RX.match(p.name))


def describe(key: str) -> dict:
    """What `app status` says about one app: its run record, corrected by the lock."""
    record = read_json(run_file(key))
    alive = host_alive(key)
    running = alive and not record.get("stopped_at")
    out = {
        "key": key,
        "running": running,
        "state": record.get("state") or "",
        "version": record.get("version") or "",
        "run_id": record.get("run_id") or "",
        "unpublished": bool(record.get("unpublished")) or dev_file(key).exists(),
        "pid": record.get("pid") if running else None,
        "started_at": record.get("started_at"),
        "stopped_at": record.get("stopped_at"),
        "stop_reason": record.get("stop_reason"),
        "counts": record.get("counts") or {},
        "last_event": record.get("last_event") or "",
        "last_event_at": record.get("last_event_at"),
        "session_url": record.get("session_url") or "",
    }
    if record and not alive and not record.get("stopped_at"):
        out["stop_reason"] = HOST_DIED
    if not running:
        out["state"] = "stopped" if record else ""
    return out
