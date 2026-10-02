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
