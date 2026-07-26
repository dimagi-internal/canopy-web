"""Architecture boundary test — enforces the framework→product one-way rule.

The dependency arrow is one-way: FRAMEWORK code (the generic, agent-agnostic
substrate any agent could reuse) never imports PRODUCT code (canopy's own domain
features). PRODUCT may freely import FRAMEWORK. This is the invariant that keeps
the blend "cuttable" — see ARCHITECTURE.md for tier definitions and the rationale
behind every carve-out below.

This test is the machine-checkable shadow of ARCHITECTURE.md (Wave 0 of the
framework harvest). Pure stdlib `ast` — no Django setup, no new dependency.

If it fails you have two honest options:
  (a) you put product code in a framework app  → move it to a product app, or
  (b) you added a genuinely new allowed seam   → document it in ARCHITECTURE.md
      and add it to COMPOSITION_ROOT / ALLOWED_FILES below WITH a reason.
Never silence a violation by reclassifying an app just to make this pass.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"

# ── The tiers (canonical copy lives in ARCHITECTURE.md; keep them in sync) ──────
FRAMEWORK = {"agents", "agent_runs", "workspaces", "api", "common", "timeline", "tokens", "session_sharing", "issues", "mcp", "system", "harness", "push", "realtime", "canopy_sessions", "feedback"}
PRODUCT = {"projects",
           "walkthroughs", "reviews", "shareouts", "runs"}

# The ONE composition root allowed to import every app — it wires each app's
# router into the single NinjaAPI (analogous to a Django URLconf). A framework
# needs exactly one such seam; this is it. Not subject to the one-way rule.
COMPOSITION_ROOT = {"api"}

# File-level carve-outs: framework files that legitimately import product, each
# with a documented reason. Keep this list SHORT and justified — every entry is a
# known, accepted seam, not a TODO.
ALLOWED_FILES = {
    # The insights MCP tool is a PRODUCT tool (canopy portfolio insights) that
    # registers on the FRAMEWORK MCP server — apps/mcp/server.py imports
    # apps.mcp.tools as a registration side effect. Same composition-root shape
    # as the api router hub. Candidate for inversion (product registers its own
    # tool via AppConfig.ready) in a later wave.
    "apps/mcp/tools/insights.py",
}

# Framework files allowed to name a PRODUCT module PATH in a STRING literal (not an
# import). The import test above can't see these — the whole point of a string
# reference is to dodge a hard framework→product import — so this second gate
# catches the content leak the AST import-check is blind to. Keep it SHORT.
STRING_REF_ALLOWED = ALLOWED_FILES | {
    # The timeline registry resolves each product event-source by dotted path via
    # import_module precisely to AVOID a framework→product import; the string
    # indirection IS the documented seam (ARCHITECTURE.md, timeline row). A missing
    # product app degrades gracefully rather than crashing the framework.
    "apps/timeline/sources.py",
}

# Product app names as they'd appear inside a dotted module path in a string.
_PRODUCT_MODULE_PREFIXES = tuple(f"apps.{p}" for p in PRODUCT)


def _imported_apps(path: pathlib.Path) -> set[str]:
    """The set of local app names this module imports (via `apps.<name>`)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("apps."):
            targets.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("apps."):
                    targets.add(alias.name.split(".")[1])
    return targets


def _is_excluded(rel: str, name: str) -> bool:
    return "/migrations/" in rel or "/tests/" in rel or name.startswith("test_")


def test_framework_apps_do_not_import_product() -> None:
    violations: list[str] = []
    for app in sorted(FRAMEWORK - COMPOSITION_ROOT):
        base = APPS / app
        for path in base.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if _is_excluded(rel, path.name) or rel in ALLOWED_FILES:
                continue
            leaked = _imported_apps(path) & PRODUCT
            if leaked:
                violations.append(f"{rel} imports product app(s) {sorted(leaked)}")
    assert not violations, (
        "FRAMEWORK code must not import PRODUCT code (see ARCHITECTURE.md):\n  "
        + "\n  ".join(violations)
    )


def _product_module_strings(path: pathlib.Path) -> set[str]:
    """Product module paths (`apps.<product>...`) named in this module's string
    literals — including its docstring. Catches content the import-check can't see:
    a lazy `import_module("apps.reviews...")`, a dotted path in a registry, etc."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for prefix in _PRODUCT_MODULE_PREFIXES:
                if prefix in node.value:
                    found.add(prefix)
    return found


def test_framework_source_does_not_reference_product_modules() -> None:
    """The framework→product arrow must hold for CONTENT, not just imports. A
    framework file that names a product module in a string is coupled to product
    just as surely as one that imports it — see the ddd.py move (product run-id
    grammar that had been parked in apps/common)."""
    violations: list[str] = []
    for app in sorted(FRAMEWORK - COMPOSITION_ROOT):
        for path in (APPS / app).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if _is_excluded(rel, path.name) or rel in STRING_REF_ALLOWED:
                continue
            leaked = _product_module_strings(path)
            if leaked:
                violations.append(f"{rel} names product module(s) {sorted(leaked)} in a string")
    assert not violations, (
        "FRAMEWORK code must not reference PRODUCT modules, even as strings "
        "(see ARCHITECTURE.md). Move the code to a product app, or document a new "
        "seam in STRING_REF_ALLOWED:\n  " + "\n  ".join(violations)
    )


def test_every_app_is_classified() -> None:
    """A new app can't silently dodge the boundary — it must be tiered explicitly."""
    on_disk = {
        p.name for p in APPS.iterdir()
        if p.is_dir() and (p / "__init__.py").exists()
    }
    assert FRAMEWORK.isdisjoint(PRODUCT), "an app is in both tiers"
    unclassified = on_disk - (FRAMEWORK | PRODUCT)
    assert not unclassified, (
        f"unclassified app(s) {sorted(unclassified)} — add each to FRAMEWORK or "
        "PRODUCT here and in ARCHITECTURE.md"
    )
