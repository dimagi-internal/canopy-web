"""Outside `apps/workspaces/`, no production code names a workspace ROLE.

It asks `apps/workspaces/permissions.py` for a CAPABILITY instead
(`perms.can(user, ws, perms.CONTENT_WRITE)`), and the one table there decides
which roles hold it.

Why this is a fitness test and not a convention: the 2026-10-02 audit found the
same tier written five ways — `role in {editor, owner}`, `!= OWNER`,
`filter(role=OWNER)`, `_require_role(OWNER)`, `has_role_at_least(EDITOR)` — and
inserting `admin` between editor and owner silently changed what two of them
meant (one set refused admins every editor action; every `!= OWNER` kept them
off what they were meant to run). A role named at a call site is a decision
about the ladder made far from the ladder. With the names confined here, a new
role is one edit to `permissions.MINIMUM_ROLE`, and nothing else can disagree.

What it flags, in any non-test, non-migration module outside `apps/workspaces/`:

* `WorkspaceMembership.OWNER` / `.ADMIN` / `.EDITOR` / `.VIEWER`
* `ROLE_RANK`
* `has_role_at_least(` and `request_workspace_slugs_at_least(` — rank checks
  by role name; ask `perms.can` / `perms.request_slugs_with`
* `member_role(...)` compared with `==`, `!=`, `in` or `not in`

Reading a role to DISPLAY it (`member_role(...)` returned in a payload, or
passed to `perms.role_allows`) is fine — the rule is about deciding, not about
naming in a response.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps"

_ROLE_ATTRS = {"OWNER", "ADMIN", "EDITOR", "VIEWER"}
_BANNED_CALLS = {"has_role_at_least", "request_workspace_slugs_at_least"}


def _production_modules():
    for path in APPS.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("apps/workspaces/"):
            continue
        if "/migrations/" in rel or "/tests/" in rel or path.name.startswith("test_"):
            continue
        yield rel, path


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute):
            return f.attr
        if isinstance(f, ast.Name):
            return f.id
    return ""


def _violations(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in _ROLE_ATTRS
                and isinstance(node.value, (ast.Name, ast.Attribute))
                and getattr(node.value, "attr", getattr(node.value, "id", "")) == "WorkspaceMembership"):
            out.append((node.lineno, f"WorkspaceMembership.{node.attr}"))
        elif isinstance(node, (ast.Name, ast.Attribute)) and (
                getattr(node, "id", None) == "ROLE_RANK" or getattr(node, "attr", None) == "ROLE_RANK"):
            out.append((node.lineno, "ROLE_RANK"))
        elif _call_name(node) in _BANNED_CALLS:
            out.append((node.lineno, f"{_call_name(node)}(...)"))
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_call_name(s) == "member_role" for s in sides) and any(
                    isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops):
                out.append((node.lineno, "member_role(...) compared"))
    return out


def test_no_module_outside_workspaces_names_a_role():
    found = []
    for rel, path in _production_modules():
        src = path.read_text()
        if not re.search(r"WorkspaceMembership|ROLE_RANK|has_role_at_least|"
                         r"request_workspace_slugs_at_least|member_role", src):
            continue
        for line, what in _violations(ast.parse(src)):
            found.append(f"{rel}:{line}  {what}")
    assert not found, (
        "Decide by CAPABILITY, not by role — ask apps/workspaces/permissions.py "
        "(perms.can / perms.request_slugs_with / perms.role_allows) and add a "
        "capability there if none fits:\n  " + "\n  ".join(sorted(found))
    )


def test_the_checker_catches_each_shape():
    """The rule is only as good as its parser — prove each banned shape trips."""
    src = (
        "WorkspaceMembership.OWNER\n"
        "x = wsvc.WorkspaceMembership.EDITOR\n"
        "ROLE_RANK\n"
        "wsvc.has_role_at_least(u, w, r)\n"
        "if wsvc.member_role(u, w) != r: pass\n"
        "if member_role(u, w) in roles: pass\n"
    )
    kinds = {what for _, what in _violations(ast.parse(src))}
    assert kinds == {
        "WorkspaceMembership.OWNER", "WorkspaceMembership.EDITOR", "ROLE_RANK",
        "has_role_at_least(...)", "member_role(...) compared",
    }


def test_every_capability_maps_to_a_real_role():
    from apps.workspaces import permissions as perms
    from apps.workspaces.models import WorkspaceMembership

    assert set(perms.MINIMUM_ROLE.values()) <= set(WorkspaceMembership.ROLE_RANK)
