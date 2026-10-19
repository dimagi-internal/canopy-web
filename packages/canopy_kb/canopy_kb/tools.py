"""A knowledge base's MCP tools, transport-free: the tool list (`tool_specs`) and
the dispatcher (`call_tool`). canopy-web serves them over Streamable HTTP per
workspace project (`apps/mcp/projects.py`); the canopy CLI's `canopy kb mcp`
serves the same specs over stdio.

Tools: `kb_info`, `search`, `get`, `list_items`, `whats_new`, `table`, plus one tool per
table the manifest declares (e.g. `grants`), whose arguments are that table's
filter_columns — so a curator adding a table gives every reader a named tool for it.
"""
from __future__ import annotations

import re

from canopy_kb.hybrid import MODES, Semantic
from canopy_kb.kb import KB, KBError

PROTOCOL = "2025-06-18"

_STR = {"type": "string"}
_INT = {"type": "integer"}


def _base_tools() -> list[dict]:
    return [
        {"name": "kb_info", "description": "What this knowledge base holds: publisher, licence, item counts by type, tables, build date.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "search", "description": "Best-matching passages for a question across every source item, each with its source URL and date. Finds by words and by meaning, so a paraphrase works; put exact phrases in double quotes.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": _STR, "types": {"type": "array", "items": _STR, "description": "limit to item types (see kb_info)"},
             "since": {**_STR, "description": "YYYY-MM-DD"}, "until": {**_STR, "description": "YYYY-MM-DD"},
             "limit": {**_INT, "default": 10},
             "mode": {**_STR, "enum": list(MODES), "default": "hybrid",
                      "description": "hybrid (words and meaning, fused), lexical (words only: exact names, ids, numbers) or semantic (meaning only)"}}}},
        {"name": "get", "description": "One item's full text and metadata, by id or URL (from search results). Long items are truncated; pass passage to read one passage.",
         "inputSchema": {"type": "object", "required": ["ref"], "properties": {
             "ref": _STR, "max_chars": {**_INT, "default": 20000}, "passage": _INT,
             "grep": {**_STR, "description": "regex; return only matching lines with context (cheap way to verify a quote or number)"},
             "context": {**_INT, "default": 1}}}},
        {"name": "list_items", "description": "Items newest first, optionally by type and date.",
         "inputSchema": {"type": "object", "properties": {
             "types": {"type": "array", "items": _STR}, "since": _STR, "limit": {**_INT, "default": 50}}}},
        {"name": "whats_new", "description": "Items that appeared or changed at the publisher since a date (the refresh history).",
         "inputSchema": {"type": "object", "properties": {"since": _STR, "limit": {**_INT, "default": 100}}}},
        {"name": "table", "description": "Query any structured table by name (see kb_info): substring matches per column, date range, minimum amount, sort.",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {
             "name": _STR, "match": {"type": "object", "additionalProperties": _STR}, "text": _STR,
             "since": _STR, "until": _STR, "min_amount": {"type": "number"}, "sort": _STR,
             "limit": {**_INT, "default": 50}}}},
    ]


def _tool_name(table: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", table)[:60]


def table_tools(kb: KB) -> list[dict]:
    out = []
    for name, spec in (kb.manifest.get("tables") or {}).items():
        props = {c: {**_STR, "description": f"substring of {c}"} for c in spec.get("filter_columns") or []}
        props.update({"text": {**_STR, "description": "substring anywhere in the row"},
                      "sort": {**_STR, "description": "column name; prefix - for descending"},
                      "limit": {**_INT, "default": 50}})
        if spec.get("date_column"):
            props.update({"since": {**_STR, "description": f"{spec['date_column']} >= YYYY-MM-DD"},
                          "until": {**_STR, "description": f"{spec['date_column']} <= YYYY-MM-DD"}})
        if spec.get("amount_column"):
            props["min_amount"] = {"type": "number", "description": f"{spec['amount_column']} at least"}
        out.append({"name": _tool_name(name), "description": spec.get("description") or f"Rows of the {name} table.",
                    "inputSchema": {"type": "object", "properties": props}, "_table": name})
    return out


def call_tool(kb: KB, name: str, args: dict, tables: dict[str, dict], semantic: Semantic | None = None):
    if name == "kb_info":
        return kb.info()
    if name == "search":
        return kb.search(args["query"], args.get("types"), args.get("since", ""), args.get("until", ""),
                         int(args.get("limit", 10)), args.get("mode") or "hybrid", semantic)
    if name == "get":
        return kb.get(args["ref"], int(args.get("max_chars", 20000)), args.get("passage"),
                      args.get("grep", ""), int(args.get("context", 1)))
    if name == "list_items":
        return kb.list_items(args.get("types"), args.get("since", ""), int(args.get("limit", 50)))
    if name == "whats_new":
        return kb.whats_new(args.get("since", ""), int(args.get("limit", 100)))
    if name == "table":
        return kb.table(args["name"], args.get("match"), args.get("text", ""), args.get("since", ""), args.get("until", ""),
                        args.get("min_amount"), args.get("sort", ""), int(args.get("limit", 50)))
    if name in tables:
        tbl = tables[name]["_table"]
        cols = (kb.manifest["tables"][tbl].get("filter_columns") or [])
        match = {c: args[c] for c in cols if args.get(c)}
        return kb.table(tbl, match, args.get("text", ""), args.get("since", ""), args.get("until", ""),
                        args.get("min_amount"), args.get("sort", ""), int(args.get("limit", 50)))
    raise KBError(f"unknown tool {name!r}")


def tool_specs(kb: KB) -> list[dict]:
    """Every tool this KB offers, as MCP tool descriptions (name, description, inputSchema)."""
    return _base_tools() + [{k: v for k, v in t.items() if k != "_table"} for t in table_tools(kb)]


def run(kb: KB, name: str, args: dict, semantic: Semantic | None = None):
    """Call one tool by name: the result is JSON-serialisable; raises KBError (or
    KeyError/ValueError/TypeError on bad arguments). `semantic` is the host's
    nearest-passages lookup (`hybrid.Semantic`); without it `search` is lexical."""
    return call_tool(kb, name, args or {}, {t["name"]: t for t in table_tools(kb)}, semantic)
