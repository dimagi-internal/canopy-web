"""A route docstring is published as the OpenAPI description, so it states what the route does now.

A date, a PR or issue number or a person's name is history or gossip, not behaviour:
it goes stale and every client reads it. Maintainer rationale belongs in a `#` comment
beside the code (comments are not published). Pure stdlib (`ast`); no Django setup.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytestmark = pytest.mark.guard

APPS = pathlib.Path(__file__).resolve().parent.parent / "apps"
VERBS = {"get", "post", "put", "patch", "delete"}
# a date, a PR / issue reference (`repo#123`, `PR #123`, `(#123)`), or a person's name
HISTORY = re.compile(
    r"\b20\d\d-\d\d-\d\d\b|[\w-]*#\d+|\bPR\s*\d+\b|\b(?:Jonathan|Aathithya|Ada|Hal)\b")


def _is_route(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
        isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in VERBS
        for d in node.decorator_list)


def test_route_docstrings_carry_no_date_pr_or_name():
    hits = []
    for path in sorted(APPS.glob("*/api*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not _is_route(node):
                continue
            doc = ast.get_docstring(node, clean=False) or ""
            hits += [f"  {path.relative_to(APPS.parent)}:{node.lineno} {node.name}: {m.group(0)!r}"
                     for m in HISTORY.finditer(doc)]
    assert not hits, (
        "A route docstring is published as the OpenAPI description, so it must not carry a "
        "date, a PR/issue number or a person's name:\n" + "\n".join(hits)
        + "\nPut maintainer rationale (why, when, who) in a `#` comment above or inside the "
        "function, and keep the docstring to what the route does now."
    )
