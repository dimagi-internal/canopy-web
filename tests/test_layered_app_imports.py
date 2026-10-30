"""Layered app imports — a lower layer never imports a higher one, and the known
exceptions can only shrink.

`tests/test_architecture_boundary.py` holds the framework→product arrow. This holds
the arrows INSIDE the framework: every app sits in one layer of `LAYERS`, and an
import from a lower layer to a higher one is a violation. Imports inside a function
count exactly like module-level ones — a lazy import hides a cycle from Python, not
the dependency.

The violations that exist today are frozen in `FROZEN` as `(src, dst): count` — a
RATCHET. The test fails when

  * a new upward edge appears, or a frozen edge's count grows — fix the import
    (move the code down, pass a callback, use a signal or a string registry), or, if
    the map itself is wrong, move the app between layers here and in ARCHITECTURE.md;
  * a frozen edge's count drops or the edge disappears — you fixed something, so
    record it: the failure prints the exact line to change or delete.

Regenerate the whole table (after a rebase, or a reviewed map change) with
`uv run python -m tests.test_layered_app_imports --write`. Counting is one per import
statement, test files and migrations excluded (`tests/_source.py`). Layers and how
to use this: ARCHITECTURE.md § Layers inside the framework.
"""
from __future__ import annotations

import ast
from collections import Counter

from tests._source import ROOT, iter_app_sources


def _product_tier() -> set[str]:
    """`PRODUCT` from test_architecture_boundary.py, read rather than imported (no test
    module imports another — tests/test_no_test_module_imports.py)."""
    tree = ast.parse((ROOT / "tests" / "test_architecture_boundary.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PRODUCT" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("PRODUCT not found in tests/test_architecture_boundary.py")


# The composition root (`apps/api/api.py`, which wires every router) is its own unit
# above everything; the rest of `apps/api` is the API kit (auth, errors, pagination,
# renderers, route_gates, tenancy) and sits in the bottom layer.
API_ROOT = "api.api"

LAYERS: list[tuple[str, set[str]]] = [
    # L0 kit — infrastructure every app may use; knows no agent, turn or session.
    ("kit", {"common", "events", "workspaces", "api"}),
    # L1 core — the agent/turn/session substrate and who may reach it.
    ("core", {"agents", "harness", "canopy_sessions", "tokens", "contacts", "admission", "retention",
              "realtime", "push", "passkeys", "embedding", "mcp_apps_host", "attention", "agent_tasks"}),
    # L2 features — framework surfaces built on the core; nothing below names them.
    ("features", {"agent_runs", "capabilities", "connections", "feedback", "github_connect", "host_grants", "huddles",
                  "inbound", "issues", "mcp", "oauth_server", "plugins", "session_sharing", "slack",
                  "system", "timeline", "threads", "approvals"}),
    # L3 product — canopy's own features (the tiers in test_architecture_boundary).
    ("product", _product_tier()),
    # L4 the composition root.
    ("root", {API_ROOT}),
]
LAYER = {unit: i for i, (_, units) in enumerate(LAYERS) for unit in units}

# Upward imports that exist today, one count per import statement. Only shrinks.
FROZEN: dict[tuple[str, str], int] = {
    # BEGIN FROZEN
    ("admission", "inbound"): 1,
    ("agents", "capabilities"): 1,
    ("agents", "github_connect"): 2,
    ("agents", "huddles"): 5,
    ("agents", "issues"): 1,
    ("agents", "oauth_server"): 1,
    ("agents", "plugins"): 1,
    ("agents", "slack"): 3,
    ("agents", "timeline"): 1,
    ("common", "canopy_sessions"): 2,
    ("common", "contacts"): 2,
    ("common", "harness"): 3,
    ("common", "realtime"): 1,
    ("common", "tokens"): 1,
    ("contacts", "host_grants"): 1,
    ("embedding", "host_grants"): 8,
    ("events", "agents"): 4,
    ("events", "api.api"): 1,
    ("events", "canopy_sessions"): 5,
    ("events", "contacts"): 2,
    ("events", "harness"): 6,
    ("events", "inbound"): 1,
    ("events", "mcp"): 2,
    ("harness", "agent_runs"): 1,
    ("harness", "capabilities"): 1,
    ("harness", "huddles"): 4,
    ("harness", "session_sharing"): 2,
    ("harness", "slack"): 1,
    ("harness", "threads"): 1,
    ("mail", "api.api"): 1,
    ("mcp", "api.api"): 1,
    ("mcp_apps_host", "host_grants"): 2,
    ("mcp_apps_host", "mcp"): 1,
    ("push", "host_grants"): 1,
    ("retention", "session_sharing"): 1,
    ("retention", "slack"): 1,
    ("workspaces", "admission"): 6,
    ("workspaces", "agents"): 3,
    ("workspaces", "attention"): 1,
    ("workspaces", "canopy_sessions"): 1,
    ("workspaces", "harness"): 8,
    ("workspaces", "push"): 1,
    ("workspaces", "realtime"): 1,
    ("workspaces", "retention"): 7,
    # END FROZEN
}


def _unit(module: str) -> str | None:
    """The layered unit a dotted module belongs to, or None if it is not an app."""
    parts = module.split(".")
    if parts[0] != "apps" or len(parts) < 2:
        return None
    if parts[1] == "api" and len(parts) > 2 and parts[2] == "api":
        return API_ROOT
    return parts[1]


def _resolve(node: ast.ImportFrom, rel: str) -> list[str]:
    """Absolute module(s) an ImportFrom names, resolving relative imports from `rel`."""
    if node.level == 0:
        base = node.module or ""
    else:
        pkg = rel[:-3].split("/")[:-1]  # the module's package
        pkg = pkg[: len(pkg) - (node.level - 1)]
        base = ".".join(pkg + ([node.module] if node.module else []))
    if node.module is None or base == "apps":  # `from . import x` / `from apps import x`
        return [f"{base}.{a.name}" for a in node.names]
    return [base]


def upward_imports() -> Counter[tuple[str, str]]:
    """Every import from a lower layer to a higher one, counted per (src, dst)."""
    found: Counter[tuple[str, str]] = Counter()
    for src in iter_app_sources(("apps",)):
        src_unit = _unit(src.rel[:-3].replace("/", "."))
        if src_unit is None or src_unit == API_ROOT:
            continue
        for node in ast.walk(src.tree):
            if isinstance(node, ast.ImportFrom):
                modules = _resolve(node, src.rel)
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                continue
            for dst_unit in {_unit(m) for m in modules} - {None, src_unit}:
                if LAYER[src_unit] < LAYER[dst_unit]:
                    found[(src_unit, dst_unit)] += 1
    return found


def _line(edge: tuple[str, str], count: int) -> str:
    return f'    ("{edge[0]}", "{edge[1]}"): {count},'


def test_every_app_has_a_layer() -> None:
    on_disk = {p.name for p in (ROOT / "apps").iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    all_units = [u for _, units in LAYERS for u in units]
    assert len(all_units) == len(set(all_units)), "an app is in two layers"
    missing = on_disk - set(all_units)
    gone = set(all_units) - on_disk - {API_ROOT}
    assert not missing, f"app(s) {sorted(missing)} have no layer — add each to LAYERS here and in ARCHITECTURE.md"
    assert not gone, f"LAYERS names app(s) {sorted(gone)} that no longer exist"


def test_no_new_upward_imports() -> None:
    found = upward_imports()
    grown = {e: n for e, n in found.items() if n > FROZEN.get(e, 0)}

    def where(e: tuple[str, str]) -> str:
        return f"{LAYERS[LAYER[e[0]]][0]} → {LAYERS[LAYER[e[1]]][0]}"

    assert not grown, (
        "A lower layer imports a higher one (ARCHITECTURE.md § Layers inside the framework). "
        "Remove the import — move the code down a layer, pass a callback, or use a signal "
        "or string registry — rather than raising the frozen count:\n"
        + "\n".join(f"  {s} → {d} ({where((s, d))}): {n} import(s), {FROZEN.get((s, d), 0)} allowed"
                    for (s, d), n in sorted(grown.items()))
    )


def test_frozen_upward_imports_only_shrink() -> None:
    found = upward_imports()
    shrunk = {e: found.get(e, 0) for e, n in FROZEN.items() if found.get(e, 0) < n}
    assert not shrunk, (
        "Upward imports were removed — thank you. Ratchet FROZEN in "
        "tests/test_layered_app_imports.py down to match:\n"
        + "\n".join(f"  delete  {_line(e, FROZEN[e]).strip()}" if n == 0
                    else f"  change  {_line(e, FROZEN[e]).strip()}  →  {n}"
                    for e, n in sorted(shrunk.items()))
    )


if __name__ == "__main__":  # `python -m tests.test_layered_app_imports [--write]`
    import re
    import sys

    table = "\n".join(_line(e, n) for e, n in sorted(upward_imports().items()))
    if "--write" not in sys.argv:
        print(table)
    else:
        path = ROOT / "tests" / "test_layered_app_imports.py"
        path.write_text(re.sub(r"(# BEGIN FROZEN\n).*?(\n    # END FROZEN)",
                               lambda m: m.group(1) + table + m.group(2), path.read_text(), flags=re.S))
