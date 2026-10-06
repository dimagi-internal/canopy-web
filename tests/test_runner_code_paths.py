"""The runner-staleness sha is computed in four places; they must name the same paths.

The server's expectation (deploy-labs.yml), an install's stamp (install-runner.sh,
install-runner.ps1) and a source checkout's live answer (provenance.CODE_PATHS)
are compared as equals. If one names a path the others do not, the shas differ on
the next commit to that path and the box reads stale forever — or a change ships
nowhere because nothing marked any box stale, which is how the 2026-10-06 mark sat
installed on every laptop and visible on none.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _provenance_paths() -> tuple[str, ...]:
    tree = ast.parse((ROOT / "runner/canopy_runner/canopy_runner/provenance.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CODE_PATHS" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("provenance.CODE_PATHS not found")


def _quoted(pattern: str, path: str) -> tuple[str, ...]:
    m = re.search(pattern, (ROOT / path).read_text())
    assert m, f"{path}: runner code paths not found"
    return tuple(m.group(1).split())


def test_every_place_names_the_same_runner_code_paths():
    expected = _provenance_paths()
    assert _quoted(r'RUNNER_CODE_PATHS="([^"]+)"', "runner/canopy_runner/scripts/install-runner.sh") == expected
    assert _quoted(
        r'RUNNER_CODE="([^"]+)"', ".github/workflows/deploy-labs.yml"
    ) == expected
    ps1 = (ROOT / "runner/canopy_runner/scripts/install-runner.ps1").read_text()
    m = re.search(r"\$RUNNER_CODE_PATHS = @\(([^)]+)\)", ps1)
    assert m and tuple(re.findall(r'"([^"]+)"', m.group(1))) == expected


def test_the_paths_exist():
    for p in _provenance_paths():
        assert (ROOT / p).exists(), p
