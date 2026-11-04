"""Pack a folder into a deterministic zip, and unpack one safely.

The same folder must give the same sha256 on any computer, because the server refuses
the same version with different bytes. So the zip is STORED, not deflated: the deflated
bytes depend on which zlib the interpreter was built with, and runner bundles differ.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from . import manifest as manifest_mod

#: The server's limit: a package travels base64-encoded in a JSON body.
MAX_PACKED = int(1.5 * 1024 * 1024)
MAX_UNPACKED = 20 * 1024 * 1024
MAX_FILES = 200
MANIFEST = "manifest.json"
LEFT_OUT_DIRS = {"tests", "__pycache__"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class PackageError(ValueError):
    """A package or folder breaks a rule. Says which, in words a person can act on."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _refuse_binary(name: str, data: bytes) -> None:
    if b"\0" in data:
        raise PackageError(f"{name} is a binary file; packages hold source only")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageError(f"{name} is not UTF-8 text; packages hold source only") from None


def _check_total(files: int, total: int) -> None:
    if files > MAX_FILES:
        raise PackageError(f"more than {MAX_FILES} files")
    if total > MAX_UNPACKED:
        raise PackageError(f"more than {MAX_UNPACKED // (1024 * 1024)} MB unpacked")


def _left_out(name: str) -> bool:
    return name.startswith(".") or name in LEFT_OUT_DIRS


def collect(folder: Path) -> list[tuple[str, bytes, bool]]:
    """(posix name, bytes, executable) for each packaged file, sorted by name."""
    root = Path(folder)
    found: list[tuple[str, bytes, bool]] = []
    total = 0
    for here, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            if not _left_out(name) and (Path(here) / name).is_symlink():
                raise PackageError(f"{name} is a symlink; packages cannot hold symlinks")
        dirs[:] = sorted(d for d in dirs if not _left_out(d))
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = Path(here) / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PackageError(f"{rel} is a symlink; packages cannot hold symlinks")
            if not stat.S_ISREG(info.st_mode):
                raise PackageError(f"{rel} is not a regular file")
            total += info.st_size
            _check_total(len(found) + 1, total)
            data = path.read_bytes()
            _refuse_binary(rel, data)
            found.append((rel, data, bool(info.st_mode & stat.S_IXUSR)))
    return sorted(found, key=lambda item: item[0])


def read_manifest(folder: Path) -> dict:
    path = Path(folder) / MANIFEST
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise PackageError(f"no {MANIFEST} in {folder}") from None
    except ValueError as exc:
        raise PackageError(f"{MANIFEST} is not valid JSON: {exc}") from None
    try:
        return manifest_mod.validate(raw)
    except ValueError as exc:
        raise PackageError(f"{MANIFEST}: {exc}") from None


def pack(folder: Path) -> tuple[dict, bytes]:
    """(validated manifest, zip bytes). Every varying zip field is fixed."""
    manifest = read_manifest(folder)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data, executable in collect(folder):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(info, data)
    packed = buffer.getvalue()
    if len(packed) > MAX_PACKED:
        raise PackageError(f"package is over {MAX_PACKED / (1024 * 1024)} MB")
    return manifest, packed


def _safe_name(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\0" in name or name.startswith("/"):
        raise PackageError(f"unsafe path in package: {name!r}")
    path = PurePosixPath(name)
    if any(part in ("..", "") for part in path.parts) or ":" in path.parts[0]:
        raise PackageError(f"unsafe path in package: {name!r}")
    return path


def _extract(data: bytes, dest: Path) -> list[tuple[Path, bool]]:
    """Writes the files into dest, which must exist and be empty."""
    if len(data) > MAX_PACKED:
        raise PackageError(f"package is over {MAX_PACKED / (1024 * 1024)} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackageError("not a zip file") from None
    written: list[tuple[Path, bool]] = []
    seen: set[str] = set()
    total = 0
    with archive:
        for info in archive.infolist():
            path = _safe_name(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PackageError(f"{info.filename} is a symlink; packages cannot hold symlinks")
            if info.is_dir():
                continue
            # Compared folded, because a Mac's disk would merge the two names.
            if info.filename.casefold() in seen:
                raise PackageError(f"{info.filename} appears twice in the package")
            seen.add(info.filename.casefold())
            # Counted from what actually comes out: a zip's own sizes can lie.
            with archive.open(info) as source:
                content = source.read(MAX_UNPACKED - total + 1)
            total += len(content)
            _check_total(len(written) + 1, total)
            _refuse_binary(info.filename, content)
            target = dest / Path(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "xb") as out:
                out.write(content)
            written.append((target, bool(mode & stat.S_IXUSR)))
    return written


def remove_tree(path: Path) -> None:
    """Remove an unpacked version, which is read-only on purpose."""
    path = Path(path)
    if not path.exists():
        return
    for here, dirs, _files in os.walk(path):
        os.chmod(here, 0o700)
        for name in dirs:
            os.chmod(Path(here) / name, 0o700)
    shutil.rmtree(path, ignore_errors=True)


def unpack(data: bytes, target: Path) -> None:
    """Unpack beside `target`, rename it in, then make it read-only. Any failure leaves
    `target` as it was."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    remove_tree(staging)
    staging.mkdir(mode=0o700)
    try:
        written = _extract(data, staging)
        for path, executable in written:
            os.chmod(path, 0o555 if executable else 0o444)
        remove_tree(target)
        os.rename(staging, target)
    except BaseException:
        remove_tree(staging)
        raise
    for here, dirs, _files in os.walk(target, topdown=False):
        for name in dirs:
            os.chmod(Path(here) / name, 0o555)
    os.chmod(target, 0o555)
