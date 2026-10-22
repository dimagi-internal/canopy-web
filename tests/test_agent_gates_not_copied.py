"""No module reaches into `apps.agents.api` for a private helper.

"Resolve an agent by slug, gate it by tenancy, else 404" lives in ONE place,
`apps/agents/gates.py`. It used to be hand-copied into each api module (because
"api modules must not depend on each other") while other modules imported the
private `_get_agent_or_404` / `_agent_for_admin` / … from `apps.agents.api`
anyway — and the copies drifted. A private name imported from another module is
the first step of that again, so it fails here: ask `apps.agents.gates`, or make
the helper public where it belongs.

Tests are exempt: a unit test may poke at a module's internals.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = "apps.agents.api"


def _module_of(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _resolve(node: ast.ImportFrom, here: str) -> str:
    if not node.level:
        return node.module or ""
    package = here.split(".")[: -node.level]
    return ".".join([*package, node.module] if node.module else package)


def _offenders() -> list[str]:
    found = []
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        here = _module_of(path)
        if here == TARGET:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _resolve(node, here) == TARGET:
                for alias in node.names:
                    if alias.name.startswith("_"):
                        found.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.Attribute) and node.attr.startswith("_") and not node.attr.startswith("__"):
                # `from apps.agents import api as agents_api; agents_api._helper(...)`
                if isinstance(node.value, ast.Name) and node.value.id in _api_aliases(tree, here):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno} uses api.{node.attr}")
    return found


def _api_aliases(tree: ast.AST, here: str) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _resolve(node, here) == "apps.agents":
            names |= {a.asname or a.name for a in node.names if a.name == "api"}
        elif isinstance(node, ast.Import):
            names |= {a.asname for a in node.names if a.name == TARGET and a.asname}
    return names


def test_no_module_imports_a_private_agents_api_helper():
    offenders = _offenders()
    assert not offenders, (
        "These modules reach into apps.agents.api for a private helper. Gate an agent "
        "with apps.agents.gates (agent_for_read / agent_for_work / agent_for_admin), "
        "or make the helper public:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_sees_a_private_import():
    """The walker is not vacuous: a relative and an absolute private import both resolve."""
    rel = ast.parse("from .api import _get_agent_or_404").body[0]
    absolute = ast.parse("from apps.agents.api import _agent_for_admin").body[0]
    assert _resolve(rel, "apps.agents.fleet_api") == TARGET
    assert _resolve(absolute, "apps.mail.api") == TARGET
