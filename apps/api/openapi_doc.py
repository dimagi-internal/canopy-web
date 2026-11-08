"""Corrections to the OpenAPI document Ninja builds, made in one place.

1. **Request text holds no NUL.** PostgreSQL refuses `\\x00` in `text`,
   `varchar` and `jsonb`, so no string a caller sends can be stored with one,
   and a request carrying one is answered 400 "Value cannot be stored"
   (`apps/api/api.py::_on_out_of_range`). Pydantic cannot put that on every
   `str` field at once, so the document says it here: every free-text string
   reachable from an operation's parameters or request body gets a `pattern`
   that excludes NUL, and every free-form object's keys get the same
   `propertyNames`. A string that already has a `pattern`, `enum`, `const` or
   `format` is left alone; its own rule already decides what it holds. The
   pattern is documentation the database enforces: it changes no types
   (`generated.ts` does not carry `pattern`) and no behaviour.

2. **Free-form JSON holds no NUL either**, at any depth, in a value or a key.
   No plain JSON Schema keyword reaches inside a value typed as any JSON, so
   that is enforced once, when the body is read (`apps/api/nul_guard.py`), with
   the same 400 — not declared on every operation that takes one.

3. **A declared refusal is problem+json.** A route that declares the refusal
   a well-formed request can still get (`response={201: X, **refusals(422)}`,
   for a rule across fields or against stored state that JSON Schema cannot
   state) answers it as `application/problem+json`, like every error here.
   Ninja documents every response as `application/json`; this says what is
   actually sent.
"""
from __future__ import annotations

from typing import Any

NO_NUL_PATTERN = r"^[^\x00]*$"

_PROBLEM_REF = "#/components/schemas/Problem"
_PROBLEM_JSON = "application/problem+json"
_REF_PREFIX = "#/components/schemas/"
_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_ONES = ("items", "additionalProperties", "not")
_ALREADY_CONSTRAINED = ("pattern", "enum", "const", "format")


def correct(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply the corrections to ``schema``, in place, and return it."""
    components = schema.get("components", {}).get("schemas", {})
    marked: set[str] = set()
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            inputs = [p.get("schema") for p in operation.get("parameters", [])]
            inputs += [m.get("schema") for m in
                       (operation.get("requestBody") or {}).get("content", {}).values()]
            for node in inputs:
                _refuse_nul(node, components, marked)
            _problems_as_problem_json(operation.setdefault("responses", {}))
    return schema


def _problems_as_problem_json(responses: dict[Any, Any]) -> None:
    for status, response in responses.items():
        if not str(status).startswith(("4", "5")):
            continue
        content = response.get("content") or {}
        body = content.get("application/json")
        schema = (body or {}).get("schema", {})
        # Problem itself, or a union whose members are problems (`DeviceProofRequiredOut | Problem`).
        if body and (schema.get("$ref") == _PROBLEM_REF
                     or _PROBLEM_REF in [m.get("$ref") for m in schema.get("anyOf", [])]):
            response["content"] = {_PROBLEM_JSON: content.pop("application/json"), **content}


def _children(node: dict[str, Any]) -> list[Any]:
    kids: list[Any] = []
    for key in _SUBSCHEMA_LISTS:
        kids += node.get(key, []) or []
    kids += [node.get(key) for key in _SUBSCHEMA_ONES]
    kids += list((node.get("properties") or {}).values())
    return kids


def _refuse_nul(node: Any, components: dict[str, Any], marked: set[str]) -> None:
    if not isinstance(node, dict):
        return
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith(_REF_PREFIX):
        name = ref[len(_REF_PREFIX):]
        if name not in marked:
            marked.add(name)
            _refuse_nul(components.get(name), components, marked)
        return
    if _is_string(node) and not any(key in node for key in _ALREADY_CONSTRAINED):
        node["pattern"] = NO_NUL_PATTERN
    if (node.get("type") == "object" and node.get("additionalProperties", True) is not False
            and "propertyNames" not in node):
        # A free-form object's KEYS are stored too (jsonb), and are text.
        node["propertyNames"] = {"pattern": NO_NUL_PATTERN}
    for kid in _children(node):
        _refuse_nul(kid, components, marked)


def _is_string(node: dict[str, Any]) -> bool:
    kind = node.get("type")
    return kind == "string" or (isinstance(kind, list) and "string" in kind)
