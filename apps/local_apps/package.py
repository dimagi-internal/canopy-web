"""Reading a submitted package without ever running it: limits, paths and facts.

The facts are hints for a person reviewing the code, not a verdict: a program can
hide an import from any scan. They exist so the reviewer's eye lands on the lines
that decide whether the manifest's disclosure is honest.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass

from . import manifest as mf

MAX_PACKED = 5 * 1024 * 1024
MAX_UNPACKED = 20 * 1024 * 1024
MAX_FILES = 200
MAX_DIFF_LINES = 4000
_NETWORK = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib", "imaplib", "poplib", "requests",
            "httpx", "aiohttp", "websockets"}
_PROCESS = {"subprocess", "multiprocessing", "pty", "pexpect"}
_RISKY_MODULES = {"ctypes", "pickle", "marshal", "importlib", "runpy"}
_RISKY_CALLS = {"eval", "exec", "compile", "__import__"}
_OS_PROCESS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "fork"}


class BadPackage(ValueError):
    """Why this package can't be accepted, in words for its author."""


@dataclass(frozen=True)
class Package:
    sha256: str
    size: int
    manifest: dict
    files: dict[str, bytes]


def _safe_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    parts = name.split("/")
    if name.startswith("/") or "\\" in name or ".." in parts or not all(parts):
        raise BadPackage(f"'{name}' is not a plain path inside the package")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise BadPackage(f"'{name}' is a symlink")
    return name


def read(data: bytes) -> Package:
    """Check a package's bytes and return what's in it, or raise BadPackage."""
    if len(data) > MAX_PACKED:
        raise BadPackage(f"the package is over {MAX_PACKED // (1024 * 1024)} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BadPackage("the package is not a zip file") from exc
    infos = [i for i in archive.infolist() if not i.is_dir()]
    if len(infos) > MAX_FILES:
        raise BadPackage(f"the package has over {MAX_FILES} files")
    # Declared sizes first, so a zip bomb is refused before anything is inflated.
    if sum(i.file_size for i in infos) > MAX_UNPACKED:
        raise BadPackage(f"the package unpacks to over {MAX_UNPACKED // (1024 * 1024)} MB")
    files: dict[str, bytes] = {}
    for info in infos:
        name = _safe_name(info)
        if name in files:
            raise BadPackage(f"'{name}' appears twice")
        with archive.open(info) as handle:
            body = handle.read(MAX_UNPACKED + 1)
        if len(body) != info.file_size:
            raise BadPackage(f"'{name}' is not the size it says")
        if b"\0" in body:
            raise BadPackage(f"'{name}' looks like a binary; a package holds only code a reviewer can read")
        try:
            body.decode()
        except UnicodeDecodeError as exc:
            raise BadPackage(f"'{name}' is not UTF-8 text") from exc
        files[name] = body
    if "manifest.json" not in files:
        raise BadPackage("the package has no manifest.json at its top level")
    try:
        manifest = mf.validate(json.loads(files["manifest.json"]))
    except ValueError as exc:  # includes a JSON decode error
        raise BadPackage(f"manifest.json: {exc}") from exc
    entry = manifest["entry"]
    program = entry[1] if entry[0] == "{python}" and len(entry) > 1 else None
    if program is not None and program not in files:
        raise BadPackage(f"the manifest runs '{program}', which isn't in the package")
    return Package(hashlib.sha256(data).hexdigest(), len(data), manifest, files)


def _imports_and_calls(source: str) -> tuple[set[str], set[str]]:
    tree = ast.parse(source)
    modules: set[str] = set()
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _RISKY_CALLS:
                calls.add(func.id)
            elif (isinstance(func, ast.Attribute) and func.attr in _OS_PROCESS
                  and isinstance(func.value, ast.Name) and func.value.id == "os"):
                calls.add(f"os.{func.attr}")
    return modules, calls


def facts(pkg: Package) -> dict:
    """What a reviewer should look at first: files, and what reaches outside."""
    network: dict[str, list[str]] = {}
    process: dict[str, list[str]] = {}
    risky: dict[str, list[str]] = {}
    unparsed: list[str] = []
    for name, body in sorted(pkg.files.items()):
        if not name.endswith(".py"):
            continue
        try:
            modules, calls = _imports_and_calls(body.decode())
        except (SyntaxError, ValueError):
            unparsed.append(name)
            continue
        for module in sorted(modules):
            for names, target in ((_NETWORK, network), (_PROCESS, process), (_RISKY_MODULES, risky)):
                if module in names:
                    target.setdefault(module, []).append(name)
        for call in sorted(calls):
            (process if call.startswith("os.") else risky).setdefault(call, []).append(name)
    return {"files": [{"path": n, "size": len(b)} for n, b in sorted(pkg.files.items())],
            "network": network, "process": process, "risky": risky, "unparsed": unparsed}


def diff(old: dict[str, bytes] | None, new: dict[str, bytes]) -> list[dict]:
    """Per-file unified diff from the last release (or, for a first release,
    every file as added), so review means reading what changed."""
    old = old or {}
    out = []
    budget = MAX_DIFF_LINES
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        lines = list(difflib.unified_diff((before or b"").decode().splitlines(), (after or b"").decode().splitlines(),
                                          fromfile=name, tofile=name, lineterm="", n=3))
        status = "added" if before is None else "removed" if after is None else "changed"
        truncated = len(lines) > budget
        out.append({"path": name, "status": status, "lines": lines[:budget], "truncated": truncated})
        budget = max(0, budget - len(lines))
    return out
