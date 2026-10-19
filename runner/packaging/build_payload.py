#!/usr/bin/env python3
"""Build the self-contained runner runtime for ONE release platform.

    python runner/packaging/build_payload.py --platform macos-arm64 --out build/payload-macos-arm64

The output directory is what an installer ships, unchanged:

    <out>/release.json                 {sha, platform, built_at, committed_at, python, node, acp_adapter}
    <out>/python/                      python-build-standalone CPython, the runner + deps in site-packages
    <out>/node/                        Node.js (macOS: bin/node, bin/npm; Windows: node.exe, npm.cmd)
    <out>/acp/                         `npm install -g --prefix` of the ACP adapter
    <out>/bin/canopy-runner[.cmd]      the launch shim

macOS adds bin/canopy-device-key in build_app.sh (it is compiled there, per arch).
Nothing here is fetched at install time — that is the point (spec decision 5).

Provenance is computed and stamped exactly as install-runner.sh does it: the last
commit touching the runner's code paths (read from provenance.CODE_PATHS, the list
tests/test_runner_code_paths.py keeps equal to deploy-labs.yml's), stamped into a
TEMP copy of the sources, never the checkout.

Cross builds: when the host cannot run the target's Python (macos-x86_64 on an
arm64 Mac without Rosetta, windows-x86_64 anywhere but Windows) the packages are
installed with `uv pip install --target … --python-platform …` and npm is told the
target `--os/--cpu`; bytecode is then not precompiled. CI builds every platform
natively, so cross builds are a convenience for local inspection only.

Needs: git, uv (wheels; and installs for cross builds), and on a native build
nothing else — the bundled Python's pip and the bundled Node's npm do the rest.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import platform as host_platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PLATFORMS = ("macos-arm64", "macos-x86_64", "windows-x86_64")
# The four in-repo packages the runner is built from, in dependency order (as
# install-runner.sh builds them).
PACKAGES = ("packages/canopy_cron", "packages/canopy_transcript", "runner/canopy_acp", "runner/canopy_runner")
RUNNER_SRC = "runner/canopy_runner/canopy_runner"
# Never copied into the temp build tree: a developer's local sidecar deps, build
# dirs and caches would otherwise be vacuumed into a wheel.
_IGNORE = shutil.ignore_patterns("node_modules", ".build", "__pycache__", ".venv", "dist", "build",
                                 "*.egg-info", ".pytest_cache", ".ruff_cache", ".mypy_cache")


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("    $ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


# ── provenance ────────────────────────────────────────────────────────────────


def code_paths() -> list[str]:
    """provenance.CODE_PATHS, parsed rather than imported (the runner package is
    not importable from the CI's Python, and a fifth copy of the list would drift)."""
    tree = ast.parse((REPO / RUNNER_SRC / "provenance.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CODE_PATHS" for t in node.targets):
            return list(ast.literal_eval(node.value))
    raise SystemExit("provenance.CODE_PATHS not found")


def resolve_provenance(ref: str) -> tuple[str, int]:
    paths = code_paths()
    git = ["git", "-C", str(REPO), "log", "-1", ref, "--"]
    sha = subprocess.run(git[:4] + ["--format=%H"] + git[4:] + paths, capture_output=True, text=True).stdout.strip()
    ts = subprocess.run(git[:4] + ["--format=%ct"] + git[4:] + paths, capture_output=True, text=True).stdout.strip()
    if not sha:
        raise SystemExit("could not resolve the runner source sha (shallow clone? CI needs fetch-depth: 0)")
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", *paths],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("WARNING: the runner code paths have uncommitted changes; this build is stamped "
              f"{sha[:12]} but carries code that commit does not have:\n{dirty}", file=sys.stderr)
    return sha, int(ts or 0)


def stage_sources(tmp: Path, sha: str, built_at: str, committed_at: int, plat: str) -> Path:
    src = tmp / "src"
    for pkg in PACKAGES:
        shutil.copytree(REPO / pkg, src / pkg, ignore=_IGNORE, symlinks=True)
    (src / RUNNER_SRC / "_build_info.py").write_text(
        '"""Build provenance, stamped by runner/packaging/build_payload.py. Generated — do not edit."""\n'
        "from __future__ import annotations\n\n"
        f'SHA = "{sha}"\n'
        f'BUILT_AT = "{built_at}"\n'
        f"COMMITTED_AT = {committed_at}\n"
        f'PLATFORM = "{plat}"\n', encoding="utf-8")
    return src


def build_wheels(src: Path, dist: Path) -> dict[str, Path]:
    log("building wheels")
    for pkg in PACKAGES:
        run(["uv", "build", "--quiet", "--wheel", "-o", dist, src / pkg])
    wheels = {}
    for name in ("canopy_cron", "canopy_transcript", "canopy_acp", "canopy_runner"):
        found = sorted(dist.glob(f"{name}-*.whl"))
        if len(found) != 1:
            raise SystemExit(f"expected one {name} wheel in {dist}, found {found}")
        wheels[name] = found[0]
    return wheels


def wheel_extras(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as z:
        meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        text = z.read(meta).decode()
    return set(re.findall(r"^Provides-Extra: (\S+)$", text, re.M))


# ── downloads ─────────────────────────────────────────────────────────────────


def fetch(url: str, sha256: str, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / url.rsplit("/", 1)[1].replace("%2B", "+")
    if dest.exists() and _sha256(dest) == sha256:
        log(f"cached {dest.name}")
        return dest
    log(f"downloading {url}")
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(part, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    got = _sha256(part)
    if got != sha256:
        part.unlink()
        raise SystemExit(f"checksum mismatch for {url}: expected {sha256}, got {got}")
    part.rename(dest)
    return dest


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract(archive: Path, into: Path) -> None:
    """Extract, refusing members that escape `into` (the archives are checksummed,
    but a path check costs nothing)."""
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            root = into.resolve()
            for m in z.namelist():
                if not (into / m).resolve().is_relative_to(root):
                    raise SystemExit(f"unsafe path in {archive.name}: {m}")
            z.extractall(into)
        return
    with tarfile.open(archive) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(into, filter="data")
        else:  # pragma: no cover — Python < 3.12 without the backport
            t.extractall(into)


# ── the runtime ───────────────────────────────────────────────────────────────


def host_can_run(plat: str) -> bool:
    system, machine = sys.platform, host_platform.machine().lower()
    if plat.startswith("windows"):
        return system == "win32" and machine in ("amd64", "x86_64")
    if system != "darwin":
        return False
    if plat == "macos-arm64":
        return machine == "arm64"
    # x86_64 runs natively on Intel, and under Rosetta on Apple silicon when installed.
    if machine == "x86_64":
        return True
    return subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0


def python_paths(out: Path, plat: str) -> tuple[Path, Path]:
    if plat.startswith("windows"):
        return out / "python" / "python.exe", out / "python" / "Lib" / "site-packages"
    return out / "python" / "bin" / "python3", out / "python" / "lib" / "python3.12" / "site-packages"


def install_python(out: Path, plat: str, versions: dict, cache: Path) -> None:
    spec = versions["python"]
    f = spec["files"][plat]
    archive = fetch(spec["url"].format(release=spec["release"], file=f["file"].replace("+", "%2B")), f["sha256"], cache)
    log(f"extracting CPython {spec['version']} ({plat})")
    extract(archive, out)  # the archive's top-level directory is `python/`
    py, _ = python_paths(out, plat)
    if not py.exists():
        raise SystemExit(f"{py} missing after extracting {archive.name}")
    # Trimmed: the stdlib's own test suite and the GUI demos are never imported by
    # the runner and are a large share of an install_only tree.
    lib = out / "python" / ("Lib" if plat.startswith("windows") else "lib/python3.12")
    for junk in ("test", "idlelib", "turtledemo", "tkinter/test", "lib2to3/tests"):
        shutil.rmtree(lib / junk, ignore_errors=True)


def install_packages(out: Path, plat: str, wheels: dict[str, Path], native: bool) -> None:
    py, site = python_paths(out, plat)
    extras = wheel_extras(wheels["canopy_runner"])
    wanted = [e for e in ("realtime", "keys") if e in extras]
    reqs = [f"canopy-runner[{','.join(wanted)}] @ {wheels['canopy_runner'].as_uri()}",
            *(str(wheels[n]) for n in ("canopy_cron", "canopy_transcript", "canopy_acp"))]
    if "keys" not in extras:
        # The runner's native device key (software / TPM) needs `cryptography`; the
        # `keys` extra is how the package declares that. A runner predating the extra
        # still gets the library so the bundle is complete either way.
        print("WARNING: canopy-runner declares no `keys` extra yet — installing cryptography directly",
              file=sys.stderr)
        reqs.append("cryptography")
    find_links = str(wheels["canopy_runner"].parent)
    if native:
        log("installing the runner into the bundled Python")
        # -I: ignore PYTHON* env and the user site, so nothing on the build host
        # leaks into (or out of) the bundle's own site-packages.
        run([py, "-I", "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
             "--no-cache-dir", "--find-links", find_links, *reqs])
    else:
        log(f"cross-installing the runner for {plat} (uv pip --target)")
        target = {"macos-arm64": "aarch64-apple-darwin", "macos-x86_64": "x86_64-apple-darwin",
                  "windows-x86_64": "x86_64-pc-windows-msvc"}[plat]
        run(["uv", "pip", "install", "--target", site, "--python-platform", target, "--python-version", "3.12",
             "--find-links", find_links, *reqs])


def finish_python(out: Path, plat: str, native: bool) -> None:
    """Precompile bytecode with hash-based invalidation, so the shim can run with
    -B and nothing ever writes into the installed (on macOS: signed) tree — a .pyc
    written into a sealed app bundle breaks its signature."""
    py, site = python_paths(out, plat)
    for d in list(site.rglob("__pycache__")):
        shutil.rmtree(d, ignore_errors=True)
    # Console-script wrappers pip wrote into python/bin name the BUILD path in their
    # shebang; the shim is the only entry point, so drop them rather than ship lies.
    if plat.startswith("windows"):
        # python.exe/pythonw.exe live in python\ itself; Scripts\ holds only pip's
        # generated .exe launchers, each with the build path baked in.
        shutil.rmtree(out / "python" / "Scripts", ignore_errors=True)
    bindir = out / "python" / "bin"
    for script in bindir.glob("*"):
        if script.is_file() and not script.is_symlink() and not script.name.startswith("python") \
                and script.stat().st_size < 4096 and script.read_bytes()[:2] == b"#!":
            script.unlink()
    # ... and the links that pointed at them (2to3, pydoc3): codesign refuses a
    # bundle holding a dangling symlink.
    for link in bindir.glob("*"):
        if link.is_symlink() and not link.exists():
            link.unlink()
    if native:
        log("precompiling bytecode")
        run([py, "-I", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", site])


def install_node(out: Path, plat: str, versions: dict, cache: Path) -> None:
    spec = versions["node"]
    f = spec["files"][plat]
    archive = fetch(spec["url"].format(version=spec["version"], file=f["file"]), f["sha256"], cache)
    log(f"extracting Node.js {spec['version']} ({plat})")
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        extract(archive, Path(tmp))
        (top,) = [p for p in Path(tmp).iterdir() if p.is_dir()]
        shutil.move(str(top), out / "node")
    # Headers and docs are for building native addons / reading; the runtime needs neither.
    for junk in ("include", "share", "CHANGELOG.md", "README.md"):
        p = out / "node" / junk
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def npm_cmd(out: Path, plat: str, native: bool) -> list[str]:
    if native:
        if plat.startswith("windows"):
            return [str(out / "node" / "node.exe"), str(out / "node" / "node_modules" / "npm" / "bin" / "npm-cli.js")]
        return [str(out / "node" / "bin" / "node"), str(out / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js")]
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("a cross build needs a host npm")
    return [npm]


def npm_env(out: Path, plat: str, native: bool, cache: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["npm_config_cache"] = str(cache / "npm")
    env["npm_config_update_notifier"] = "false"
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    if native:  # lifecycle scripts must find THIS node, not whatever is on the host
        nodedir = out / "node" if plat.startswith("windows") else out / "node" / "bin"
        env["PATH"] = str(nodedir) + os.pathsep + env.get("PATH", "")
    return env


def target_os_cpu(plat: str) -> list[str]:
    os_, cpu = {"macos-arm64": ("darwin", "arm64"), "macos-x86_64": ("darwin", "x64"),
                "windows-x86_64": ("win32", "x64")}[plat]
    # Picks the right platform-specific optional deps (the Claude Agent SDK ships its
    # native binary that way) even when the host is a different arch.
    return [f"--os={os_}", f"--cpu={cpu}"]


def install_sidecar(out: Path, plat: str, native: bool, cache: Path) -> None:
    _, site = python_paths(out, plat)
    cdp = site / "canopy_runner" / "cdp"
    if not (cdp / "package.json").exists():
        raise SystemExit(f"no CDP sidecar package.json at {cdp}")
    log("installing the CDP sidecar's node deps (npm ci)")
    run([*npm_cmd(out, plat, native), "ci", "--omit=dev", *target_os_cpu(plat)], cwd=cdp,
        env=npm_env(out, plat, native, cache))


def install_adapter(out: Path, plat: str, native: bool, cache: Path, versions: dict) -> None:
    spec = versions["acp_adapter"]
    client = (REPO / "runner/canopy_acp/canopy_acp/client.py").read_text(encoding="utf-8")
    m = re.search(r'^ADAPTER_PACKAGE = "([^"]+)"', client, re.M)
    if not m or m.group(1) != spec["package"]:
        raise SystemExit(f"versions.json pins {spec['package']} but canopy_acp.client.ADAPTER_PACKAGE "
                         f"is {m and m.group(1)} — update versions.json")
    log(f"installing the ACP adapter {spec['package']}@{spec['version']}")
    run([*npm_cmd(out, plat, native), "install", "-g", "--prefix", out / "acp", "--omit=dev",
         *target_os_cpu(plat), f"{spec['package']}@{spec['version']}"], env=npm_env(out, plat, native, cache))


SH_SHIM = """#!/bin/sh
# canopy-runner — launch shim for the bundled runtime (runner/packaging/build_payload.py).
# Resolves its own location (following symlinks), puts the bundled node and the
# ACP adapter on PATH, and runs the bundled Python: -I ignores PYTHON* env and the
# user site (nothing from the host leaks in), -B never writes bytecode into the
# (signed) bundle — it ships precompiled.
self="$0"
while [ -h "$self" ]; do
  link="$(readlink "$self")"
  case "$link" in /*) self="$link" ;; *) self="$(dirname "$self")/$link" ;; esac
done
rt="$(cd "$(dirname "$self")/.." && pwd -P)"
PATH="$rt/node/bin:$rt/acp/bin:$PATH"
export PATH
exec "$rt/python/bin/python3" -I -B -m canopy_runner.main "$@"
"""

CMD_SHIM = """@echo off
rem canopy-runner - launch shim for the bundled runtime (runner\\packaging\\build_payload.py).
rem Puts the bundled node and the ACP adapter on PATH and runs the bundled Python:
rem -I ignores PYTHON* env and the user site, -B never writes bytecode (it ships precompiled).
setlocal
set "RT=%~dp0.."
set "PATH=%RT%\\node;%RT%\\acp;%PATH%"
"%RT%\\python\\python.exe" -I -B -m canopy_runner.main %*
exit /b %ERRORLEVEL%
"""


def write_shims(out: Path, plat: str) -> None:
    bindir = out / "bin"
    bindir.mkdir(exist_ok=True)
    if plat.startswith("windows"):
        (bindir / "canopy-runner.cmd").write_bytes(CMD_SHIM.replace("\n", "\r\n").encode())
    else:
        shim = bindir / "canopy-runner"
        shim.write_text(SH_SHIM, encoding="utf-8")
        shim.chmod(0o755)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", choices=PLATFORMS)
    ap.add_argument("--out", type=Path, help="output directory (must not exist, or --clean)")
    ap.add_argument("--ref", default="HEAD", help="git ref whose runner-code history names the sha (default HEAD)")
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                    / "canopy-runner-packaging", help="download cache (checksummed)")
    ap.add_argument("--clean", action="store_true", help="delete --out first if it exists")
    ap.add_argument("--provenance", action="store_true",
                    help="only print sha=… and committed_at=… (GITHUB_OUTPUT lines) and exit")
    args = ap.parse_args()
    if args.provenance:
        sha, committed_at = resolve_provenance(args.ref)
        print(f"sha={sha}\ncommitted_at={committed_at}")
        return 0
    if not args.platform or not args.out:
        ap.error("--platform and --out are required")

    out: Path = args.out.resolve()
    if out.exists():
        if not args.clean:
            raise SystemExit(f"{out} exists (pass --clean to replace it)")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    versions = json.loads((HERE / "versions.json").read_text(encoding="utf-8"))

    sha, committed_at = resolve_provenance(args.ref)
    built_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    native = host_can_run(args.platform)
    log(f"{args.platform} | runner source {sha[:12]} ({committed_at}) | {'native' if native else 'CROSS'} build")

    with tempfile.TemporaryDirectory(prefix="canopy-runner-build-") as tmpdir:
        tmp = Path(tmpdir)
        src = stage_sources(tmp, sha, built_at, committed_at, args.platform)
        wheels = build_wheels(src, tmp / "dist")
        install_python(out, args.platform, versions, args.cache)
        install_packages(out, args.platform, wheels, native)
    finish_python(out, args.platform, native)
    install_node(out, args.platform, versions, args.cache)
    install_sidecar(out, args.platform, native, args.cache)
    install_adapter(out, args.platform, native, args.cache, versions)
    write_shims(out, args.platform)

    release = {"sha": sha, "platform": args.platform, "built_at": built_at, "committed_at": committed_at,
               "python": versions["python"]["version"], "node": versions["node"]["version"],
               "acp_adapter": versions["acp_adapter"]["version"]}
    (out / "release.json").write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")
    log(f"payload ready at {out}")
    print(json.dumps(release))
    return 0


if __name__ == "__main__":
    sys.exit(main())
