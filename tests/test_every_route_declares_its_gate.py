"""Fitness test: every REST route declares the gate it enforces.

The manifest is `apps/api/route_gates.py`; read its docstring for the rule and
the vocabulary. This file only holds the manifest to the code:

1. every operation in the OpenAPI schema has an entry (a new route fails CI
   until someone writes down who may call it);
2. no entry names an operation that no longer exists (a manifest that rots is
   one nobody reads);
3. every label is in the closed vocabulary (a typo, or a new tier invented in
   passing, is a review question, not a string);
4. every write a plain member (a VIEWER) can make is listed in
   `VIEWER_MAY_MUTATE` with its reason, and nothing else is.

What it cannot check is that the labels are TRUE — that needs reading
the view, which is the reviewer's job when the entry is added. It makes sure
the entry is added.
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


def _views_by_operation_id() -> dict[str, tuple[str, str]]:
    """operationId -> (module, function name) of the view behind it."""
    api.get_openapi_schema()  # binds the routers
    out: dict[str, tuple[str, str]] = {}
    for bound in api._get_bound_routers():
        for path_view in bound.path_operations.values():
            for op in path_view.operations:
                fn = op.view_func
                out[api.get_openapi_operation_id(op)] = (fn.__module__, fn.__name__)
    return out


@pytest.fixture(scope="module")
def operations() -> dict[str, str]:
    return _schema_operations()


def test_every_route_declares_its_gate(operations):
    missing = sorted(set(operations) - set(route_gates.GATES))
    assert not missing, (
        "These routes declare no gate. Read the view, then add each to "
        "apps/api/route_gates.py::GATES with the gate it actually enforces "
        f"(see the vocabulary there): {missing}"
    )


def test_manifest_names_only_live_routes(operations):
    stale = sorted(set(route_gates.GATES) - set(operations))
    assert not stale, f"route_gates.GATES names operations that no longer exist: {stale}"
    stale_viewer = sorted(set(route_gates.VIEWER_MAY_MUTATE) - set(operations))
    assert not stale_viewer, f"VIEWER_MAY_MUTATE names operations that no longer exist: {stale_viewer}"


def test_gate_labels_are_in_the_vocabulary():
    bad = {
        op: [g for g in gates if g not in route_gates.VOCABULARY]
        for op, gates in route_gates.GATES.items()
        if not gates or not isinstance(gates, tuple) or any(g not in route_gates.VOCABULARY for g in gates)
    }
    assert not bad, f"Empty, non-tuple, or unknown gate labels: {bad}"


def test_viewer_level_writes_are_deliberate(operations):
    viewer_writes = {
        op for op, method in operations.items()
        if method in MUTATING and "member" in route_gates.GATES.get(op, ())
    }
    unlisted = sorted(viewer_writes - set(route_gates.VIEWER_MAY_MUTATE))
    assert not unlisted, (
        "These writes are open to any member, viewer included. If that is "
        "intended, add each to VIEWER_MAY_MUTATE with a one-line reason; if "
        f"not, gate the route and fix its entry: {unlisted}"
    )
    not_viewer_writes = sorted(set(route_gates.VIEWER_MAY_MUTATE) - viewer_writes)
    assert not not_viewer_writes, (
        "VIEWER_MAY_MUTATE lists operations that are not member-gated writes "
        f"(remove them): {not_viewer_writes}"
    )
    blank = sorted(op for op, why in route_gates.VIEWER_MAY_MUTATE.items() if not why.strip())
    assert not blank, f"VIEWER_MAY_MUTATE entries need a reason: {blank}"


def test_every_write_says_whether_it_is_a_setting(operations):
    """The settings audit (`apps/events/audit.py`) records exactly the routes in
    SETTINGS. A write in neither list would change something with no decision
    on whether it is audited; a write in both is a contradiction."""
    writes = {op for op, method in operations.items() if method in MUTATING}
    settings, other = route_gates.SETTINGS, set(route_gates.NOT_SETTINGS)
    unclassified = sorted(writes - settings - other)
    assert not unclassified, (
        "These write routes don't say whether they change a setting. Add each to "
        "apps/api/route_gates.py::SETTINGS (audited: every call writes a SettingChange) "
        f"or NOT_SETTINGS with its WRITE_KINDS kind. When unsure, it is a setting: {unclassified}"
    )
    both = sorted(settings & other)
    assert not both, f"In both SETTINGS and NOT_SETTINGS: {both}"
    stale = sorted((settings | other) - writes)
    assert not stale, f"SETTINGS / NOT_SETTINGS name routes that are not live writes: {stale}"
    bad = {op: k for op, k in route_gates.NOT_SETTINGS.items() if k not in route_gates.WRITE_KINDS}
    assert not bad, f"NOT_SETTINGS kinds outside WRITE_KINDS: {bad}"


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
    for name, (gates, kind) in route_gates.HAND_TOOL_GATES.items():
        assert gates and set(gates) <= route_gates.VOCABULARY, name
        assert kind == "read" or kind in route_gates.WRITE_KINDS, name


@pytest.mark.django_db
def test_plain_writes_are_routes_not_hand_written_tools():
    """Slack share and the noise sweep are routes, reaching MCP as generated
    tools under the same names — so they are gated, audited and acting-capped."""
    from apps.mcp import api_tools

    tools = _mcp_tools()
    for name in ("share_session_to_slack", "purge_session_noise", "audit_session_noise"):
        assert isinstance(tools[name], api_tools.CanopyAPITool), name
