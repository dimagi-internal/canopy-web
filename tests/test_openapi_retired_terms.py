"""The published OpenAPI text names an agent's credential by its current term.

An agent's own sign-in to canopy is its "canopy login" (the page section, the docs
and the CLI all say so). `canopy-pat` is the retired name, and it reads as a
vault key that no longer exists. Route summaries, route descriptions and schema
descriptions are what the docs page, the generated types and the MCP tool
descriptions show, so the retired term must not appear in any of them.
operationIds (and so MCP tool names) are not text and are not checked here.
"""
from __future__ import annotations

import json

from apps.api.api import api

RETIRED = ("canopy-pat",)


def _texts(node, path=""):
    """Every `summary` and `description` string in the schema, with where it sits."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("summary", "description") and isinstance(value, str):
                yield f"{path}/{key}", value
            else:
                yield from _texts(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _texts(value, f"{path}/{i}")


def test_no_route_or_schema_text_uses_a_retired_credential_term():
    schema = json.loads(json.dumps(api.get_openapi_schema()))
    hits = [
        f"{where}: {term}"
        for where, text in _texts(schema)
        for term in RETIRED
        if term in text.lower()
    ]
    assert not hits, f"use the current term ('canopy login'), not {RETIRED}:\n" + "\n".join(hits)
