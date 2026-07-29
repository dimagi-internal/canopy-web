"""The runner is INSTALLED, so what lands in the wheel is what the daemon runs.

These build a real wheel rather than asserting on pyproject text: the failure this
guards is "hatchling silently didn't include X", which is invisible to any amount
of reading the config. See
docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md.
"""
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> zipfile.ZipFile:
    if shutil.which("uv") is None:
        pytest.skip("uv not available to build a wheel")
    out = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out), str(PKG_ROOT)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"wheel build failed: {proc.stderr[-2000:]}"
    built = list(out.glob("canopy_runner-*.whl"))
    assert len(built) == 1, f"expected one wheel, got {built}"
    return zipfile.ZipFile(built[0])


def _names(wheel: zipfile.ZipFile) -> list[str]:
    return wheel.namelist()


def test_the_cdp_sidecar_ships_in_the_wheel(wheel):
    """The .mjs is CODE and must travel with the Python that shells out to it —
    otherwise an installed runner has a wrapper and no sidecar, and every turn
    needing emdash dies at the first CDP call."""
    names = _names(wheel)
    assert "canopy_runner/cdp/emdash_control.mjs" in names
    # package.json too: it is what `npm install` reads to provision the deps.
    assert "canopy_runner/cdp/package.json" in names


def test_node_modules_never_ships(wheel):
    """A DEV checkout may have ~50MB of platform-specific node_modules sitting
    next to the sidecar (the old `cd cdp && npm install` step left it there).
    Hatchling would happily vacuum it into the wheel; deps are provisioned
    per-install instead (cdp_control.ensure_sidecar_deps)."""
    assert not [n for n in _names(wheel) if "node_modules" in n]


def test_build_info_ships_so_an_install_can_be_stamped(wheel):
    """install-runner.sh overwrites this module in its temp build tree. If it
    weren't packaged, the stamp would land nowhere and every installed runner
    would report an unknown sha — silently disabling the staleness alert."""
    assert "canopy_runner/_build_info.py" in _names(wheel)


def test_the_wheel_declares_its_console_script(wheel):
    """The launchd job runs `canopy-runner`, not `python -m`. A missing entry
    point means a plist pointing at a binary that was never created."""
    entry = [n for n in _names(wheel) if n.endswith("entry_points.txt")]
    assert entry, "no entry_points.txt in the wheel"
    assert "canopy-runner" in wheel.read(entry[0]).decode()


def test_the_wheel_version_matches_the_package(wheel):
    """`dynamic = ["version"]` reads canopy_runner/__init__.py. The runner REPORTS
    that value on every heartbeat, so a build that resolved a different version
    would have the supervisor showing a version the code does not have."""
    from canopy_runner import __version__

    meta = [n for n in _names(wheel) if n.endswith("METADATA")]
    assert meta, "no METADATA in the wheel"
    assert f"Version: {__version__}" in wheel.read(meta[0]).decode()
