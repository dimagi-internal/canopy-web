"""`apps.harness.services` is not a re-export hub.

The harness's routing, claim, ledger, scheduled turns, runner administration and
drills live in their own modules; a name is imported from the module that
defines it. This fails when code imports from `apps.harness.services` a name
that services.py does not itself define (it only imports it for its own use).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "apps" / "harness" / "services.py"


def _defined_in_services() -> set[str]:
    tree = ast.parse(SERVICES.read_text())
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _imports_from_services(path: Path, tree: ast.AST):
    in_harness = path.parent == SERVICES.parent
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if (node.level == 0 and node.module == "apps.harness.services") or (
                in_harness and node.level == 1 and node.module == "services"):
            for alias in node.names:
                yield node.lineno, alias.name


def test_nothing_imports_a_moved_name_through_harness_services():
    defined = _defined_in_services()
    offenders = []
    for path in sorted((ROOT / "apps").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        text = path.read_text()
        if "services" not in text:
            continue
        for lineno, name in _imports_from_services(path, ast.parse(text)):
            if name not in defined:
                offenders.append(f"{path.relative_to(ROOT)}:{lineno} imports {name}")
    assert not offenders, (
        "import these from the module that defines them, not apps.harness.services:\n"
        + "\n".join(offenders))
