"""Fitness test: every REST route declares the gate it enforces, at the route.

The declaration is ``@gate(...)`` (`apps/api/route_gates.py`) directly above
the view's ``def``; read that module's docstring for the rule and the
vocabulary. This file only holds the declarations to the code:

1. every operation in the OpenAPI schema carries a gate (a new route fails CI
   until someone writes down who may call it);
2. every label is in the closed vocabulary (a typo, or a new tier invented in
   passing, is a review question, not a string);
3. every write a plain member (a VIEWER) can make says why (``viewer_write=``),
   and nothing else does;
4. every write is a setting (audited) or names its kind, and no read is either.

What it cannot check is that the labels are TRUE — that needs reading
the view, which is the reviewer's job when the decorator is added. It makes
sure the decorator is added.
"""
from __future__ import annotations

import json

import pytest

from apps.api import route_gates
from apps.api.api import api

MUTATING = {"post", "put", "patch", "delete"}


def _schema_operations() -> dict[str, str]:
    """operationId -> lowercase HTTP method, from the real schema."""
    schema = json.loads(json.dumps(api.get_openapi_schema(), default=str))
    ops: dict[str, str] = {}
    for item in schema["paths"].values():
        for method, op in item.items():
            ops[op["operationId"]] = method.lower()
    return ops


def declared_gates() -> dict[str, route_gates.Gate | None]:
    """operationId -> the gate its view declares (None when it declares none)."""
    api.get_openapi_schema()  # binds the routers
    out: dict[str, route_gates.Gate | None] = {}
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            for op in path_view.operations:
                out[op.operation_id or api.get_openapi_operation_id(op)] = route_gates.gate_of(op.view_func)
    return out


@pytest.fixture(scope="module")
def operations() -> dict[str, str]:
    return _schema_operations()


@pytest.fixture(scope="module")
def gates() -> dict[str, route_gates.Gate]:
    return declared_gates()


def test_every_route_declares_its_gate(operations, gates):
    assert set(gates) == set(operations)
    missing = sorted(op for op, g in gates.items() if g is None)
    assert not missing, (
        "These routes declare no gate. Read the view, then put @gate(...) "
        "(apps/api/route_gates.py) directly above its def, with the gate it actually "
        f"enforces (see the vocabulary there): {missing}"
    )


def test_gate_labels_are_in_the_vocabulary(gates):
    bad = {op: g.labels for op, g in gates.items()
           if g and (not g.labels or any(label not in route_gates.VOCABULARY for label in g.labels))}
    assert not bad, f"Empty or unknown gate labels: {bad}"


def test_viewer_level_writes_are_deliberate(operations, gates):
    viewer_writes = {
        op for op, method in operations.items()
        if method in MUTATING and gates[op] and "member" in gates[op].labels
    }
    unlisted = sorted(op for op in viewer_writes if not gates[op].viewer_write)
    assert not unlisted, (
        "These writes are open to any member, viewer included. If that is "
        "intended, give each @gate(..., viewer_write=\"<one-line reason>\"); if "
        f"not, gate the route and fix its labels: {unlisted}"
    )
    stray = sorted(op for op, g in gates.items() if g and g.viewer_write and op not in viewer_writes)
    assert not stray, f"viewer_write= on routes that are not member-gated writes (remove it): {stray}"


def test_every_write_says_whether_it_is_a_setting(operations, gates):
    """The settings audit (`apps/events/audit.py`) records exactly the routes
    declared ``setting=True``. A write that is neither a setting nor names its
    kind would change something with no decision on whether it is audited."""
    writes = {op for op, method in operations.items() if method in MUTATING}
    unclassified = sorted(op for op in writes if gates[op] and not gates[op].setting and not gates[op].kind)
    assert not unclassified, (
        "These write routes don't say whether they change a setting. Give each "
        "@gate(..., setting=True) (audited: every call writes a SettingChange) or "
        f"@gate(..., kind=\"<a WRITE_KINDS kind>\"). When unsure, it is a setting: {unclassified}"
    )
    reads = sorted(op for op, g in gates.items() if op not in writes and g and (g.setting or g.kind))
    assert not reads, f"setting= / kind= on routes that are not writes: {reads}"
    bad = {op: g.kind for op, g in gates.items() if g and g.kind and g.kind not in route_gates.WRITE_KINDS}
    assert not bad, f"kinds outside WRITE_KINDS: {bad}"


def test_declarations_match_the_retired_tables(gates):
    """MIGRATION ONLY: the decorators carry exactly what route_gates' tables held."""
    import pathlib

    snap = json.loads((pathlib.Path(__file__).parent / "fixtures" / "route_gates_snapshot.json").read_text())
    derived = {
        "gates": {op: list(g.labels) for op, g in sorted(gates.items())},
        "viewer_may_mutate": {op: g.viewer_write for op, g in sorted(gates.items()) if g.viewer_write},
        "settings": sorted(op for op, g in gates.items() if g.setting),
        "not_settings": {op: g.kind for op, g in sorted(gates.items()) if g.kind},
    }
    assert derived == snap


def _mcp_tools() -> dict:
    from asgiref.sync import async_to_sync

    from apps.mcp.server import mcp

    return {t.name: t for t in async_to_sync(mcp.list_tools)()}


@pytest.mark.django_db
def test_every_hand_written_mcp_tool_declares_its_gate():
    """A hand-written tool bypasses the route gates, the settings audit and the
    acting cap, so it must at least say who may call it — and a stale entry
    means a tool was turned into a route (good) without its row going.
    Dynamic page tools (`page_<action>`) are each open page's, per caller."""
    from apps.mcp import api_tools

    tools = {name for name, t in _mcp_tools().items()
             if not isinstance(t, api_tools.CanopyAPITool) and not name.startswith("page_")}
    declared = set(route_gates.HAND_TOOL_GATES)
    assert not sorted(tools - declared), (
        "Hand-written MCP tools with no entry in route_gates.HAND_TOOL_GATES: "
        f"{sorted(tools - declared)}. If it is a plain write, make it a REST route instead.")
    assert not sorted(declared - tools), \
        f"HAND_TOOL_GATES names tools that do not exist: {sorted(declared - tools)}"
    for name, (labels, kind) in route_gates.HAND_TOOL_GATES.items():
        assert labels and set(labels) <= route_gates.VOCABULARY, name
        assert kind == "read" or kind in route_gates.WRITE_KINDS, name


@pytest.mark.django_db
def test_plain_writes_are_routes_not_hand_written_tools():
    """Slack share and the noise sweep are routes, reaching MCP as generated
    tools under the same names — so they are gated, audited and acting-capped."""
    from apps.mcp import api_tools

    tools = _mcp_tools()
    for name in ("share_session_to_slack", "purge_session_noise", "audit_session_noise"):
        assert isinstance(tools[name], api_tools.CanopyAPITool), name
