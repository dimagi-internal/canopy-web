"""No test module imports another test module; shared fixtures live in tests/fixtures/.

Importing fixtures from a test module re-registers them in the importer (and
pyflakes flags every one as unused or redefined, so each import carried a
`# noqa`). A fixture several suites share belongs in `tests/fixtures/<area>.py`,
registered in the root conftest's `pytest_plugins`; plain helpers there are
imported by name.

A plugin fixture applies to every test that names it, so none of them may be
autouse — that would switch it on for the whole suite.
"""
from __future__ import annotations

import ast
import pathlib

import conftest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _test_modules():
    for path in sorted((ROOT / "tests").rglob("*.py")):
        yield path, ast.parse(path.read_text())
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "tests" in path.parts or path.name.startswith("test_") or path.name == "tests.py":
            yield path, ast.parse(path.read_text())


def test_no_module_imports_a_test_module():
    offenders = []
    for path, tree in _test_modules():
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                if any(part.startswith("test_") for part in mod.split(".")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} imports {mod}")
    assert not offenders, "move what is shared into tests/fixtures/<area>.py:\n" + "\n".join(offenders)


def test_plugin_fixtures_are_never_autouse():
    autouse = []
    for mod in conftest.pytest_plugins:
        path = ROOT / (mod.replace(".", "/") + ".py")
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and any(
                            k.arg == "autouse" and getattr(k.value, "value", False) for k in dec.keywords):
                        autouse.append(f"{path.relative_to(ROOT)}::{node.name}")
    assert not autouse, f"plugin fixtures must not be autouse (ask for them with usefixtures): {autouse}"
