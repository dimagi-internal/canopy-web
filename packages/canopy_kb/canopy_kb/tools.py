"""A knowledge base's MCP tools, transport-free: the tool list (`tool_specs`) and
the dispatcher (`call_tool`). canopy-web serves them over Streamable HTTP per
workspace project (`apps/mcp/projects.py`); the canopy CLI's `canopy kb mcp`
serves the same specs over stdio.

The research tools return text (`research.py`): `kb_info` (what the library holds and
how to work it), `search`, `entity`, `doc`, `read`, `grep`, `sql`. Beside them:
`get` (an item as JSON, with `grep`), `list_items`, `whats_new`, `table`, and one tool
per table the manifest declares (e.g. `grants`), whose arguments are that table's
filter_columns — so a curator adding a table gives every reader a named tool for it.
"""
from __future__ import annotations

import re

from canopy_kb.hybrid import Rerank, Semantic
from canopy_kb.kb import KB, KBError

PROTOCOL = "2025-06-18"

_STR = {"type": "string"}
_INT = {"type": "integer"}
_TYPES = {"type": "array", "items": _STR, "description": "only these item types (see kb_info)"}


def _base_tools() -> list[dict]:
    return [
        {"name": "kb_info", "description": "Start here: what this library holds (document types and date ranges, the most-covered organisations and programs, the SQL tables and their columns) and how to research in it.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "search", "description": "Documents for a question, best first, each with its catalog card (what it is, what it says) and its best-matching passages with offsets to read from. Matches words and reranks by meaning; put exact phrases in double quotes. Try several phrasings: a miss is not absence.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": _STR, "types": _TYPES,
             "entity": {**_STR, "description": "only documents about this organisation, program or country"},
             "since": {**_STR, "description": "document date >= YYYY or YYYY-MM-DD"},
             "until": {**_STR, "description": "document date <= YYYY or YYYY-MM-DD"},
             "k": {**_INT, "default": 10, "description": "documents to return (max 30)"}}}},
        {"name": "entity", "description": "An organisation, program or country: every document about it grouped by type, newest first, and its rows in the amount tables (e.g. grants). The best start for anything about a named charity or intervention.",
         "inputSchema": {"type": "object", "required": ["name"], "properties": {
             "name": _STR, "type": {**_STR, "description": "list every document of this one type"}}}},
        {"name": "doc", "description": "One document's card, numbered outline (sections with offsets and sizes), the documents it links to and is linked from, its other dated versions, and related documents. Use before reading a long document.",
         "inputSchema": {"type": "object", "required": ["ref"], "properties": {"ref": {**_STR, "description": "id or url"}}}},
        {"name": "read", "description": "A document's text: one section of its outline, or from a character offset (search results and grep give offsets). Long reads end with the offset to continue from.",
         "inputSchema": {"type": "object", "required": ["ref"], "properties": {
             "ref": {**_STR, "description": "id or url"}, "section": {**_INT, "description": "section number from doc"},
             "at": {**_INT, "default": 0, "description": "start at this character offset"},
             "chars": {**_INT, "default": 15000, "description": "how much to read (max 60000)"}}}},
        {"name": "grep", "description": "Lines matching a regex (case-insensitive), with offsets: in one document (ref), or across every document about an entity. The way to find a figure, parameter or quote in a long report or spreadsheet.",
         "inputSchema": {"type": "object", "required": ["pattern"], "properties": {
             "pattern": _STR, "ref": {**_STR, "description": "id or url of one document"},
             "entity": {**_STR, "description": "instead of ref: every document about this entity"},
             "limit": {**_INT, "default": 40}}}},
        {"name": "sql", "description": "A read-only SQL SELECT over the library's tables (kb_info lists them and their columns: the declared tables such as grants, plus items, cards, entities, item_entities, links, sections). For totals, counts, rankings and date ranges.",
         "inputSchema": {"type": "object", "required": ["query"], "properties": {
             "query": _STR, "limit": {**_INT, "default": 200}}}},
        {"name": "get", "description": "One item as JSON (metadata and text), by id or URL. With grep, only the matching lines.",
         "inputSchema": {"type": "object", "required": ["ref"], "properties": {
             "ref": _STR, "max_chars": {**_INT, "default": 20000}, "passage": _INT,
             "grep": {**_STR, "description": "regex; return only matching lines with context"},
             "context": {**_INT, "default": 1}}}},
        {"name": "list_items", "description": "Items newest first, optionally by type and date.",
         "inputSchema": {"type": "object", "properties": {
             "types": {"type": "array", "items": _STR}, "since": _STR, "limit": {**_INT, "default": 50}}}},
        {"name": "whats_new", "description": "Items that appeared or changed at the publisher since a date (the refresh history).",
         "inputSchema": {"type": "object", "properties": {"since": _STR, "limit": {**_INT, "default": 100}}}},
        {"name": "table", "description": "Query any declared table by name (see kb_info): substring matches per column, date range, minimum amount, sort.",
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


def call_tool(kb: KB, name: str, args: dict, tables: dict[str, dict], semantic: Semantic | None = None,
              rerank: Rerank | None = None):
    if name == "kb_info":
        return kb.overview()
    if name == "search":
        return kb.find(args["query"], types=args.get("types"), entity=args.get("entity", ""),
                       since=args.get("since", ""), until=args.get("until", ""), k=int(args.get("k", 10)),
                       semantic=semantic, rerank=rerank)
    if name == "entity":
        return kb.entity(args["name"], args.get("type", ""))
    if name == "doc":
        return kb.doc(args["ref"])
    if name == "read":
        sec = args.get("section")
        return kb.read(args["ref"], int(sec) if sec is not None else None, int(args.get("at", 0)),
                       int(args.get("chars", 15000)))
    if name == "grep":
        return kb.grep(args["pattern"], args.get("ref", ""), args.get("entity", ""), int(args.get("limit", 40)))
    if name == "sql":
        return kb.sql(args["query"], int(args.get("limit", 200)))
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


def run(kb: KB, name: str, args: dict, semantic: Semantic | None = None, rerank: Rerank | None = None):
    """Call one tool by name: the result is text (the research tools) or JSON-serialisable;
    raises KBError (or KeyError/ValueError/TypeError on bad arguments). `semantic` is the
    host's nearest-passages lookup (`hybrid.Semantic`); without it `search` matches words
    only. `rerank` is the host's reranker (`hybrid.Rerank`); without it, no rescoring."""
    return call_tool(kb, name, args or {}, {t["name"]: t for t in table_tools(kb)}, semantic, rerank)
