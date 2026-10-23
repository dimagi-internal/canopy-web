"""A host's kept MCP results, in miniature — test-only.

Keeping a tool's result is the HOST's MCP business, not canopy's: a host returns facts
plus a standard MCP `resource_link` to a result it keeps, and answers `resources/read`
on its own scheme as the reader. canopy only checks the link's scheme against the
host's `site.json` (`site.resource_schemes`), carries the link, and relays "it changed".

This is the smallest host that does that, so canopy's side can be driven end to end
(`tests/test_host_held_results.py`). It lives outside canopy's database on purpose: the
canary there proves canopy itself never holds a row.
"""
from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from canopy_sdk import resources

#: The `_meta` key a read's content carries about the result (the host's own name).
META_KEY = "labs/result"


class Refused(Exception):
    """A read the host will not answer: `not_found` or `forbidden`."""


@dataclass
class Kept:
    owner: str
    kind: str
    payload: dict
    mime_type: str = "application/json"


class ResultStore:
    """In-memory results under one scheme, readable by their owner and whoever `may_read` admits."""

    def __init__(self, scheme: str, *, may_read: Callable[[str | None, Kept], bool] | None = None):
        self.scheme = resources.check_scheme(scheme)
        self.may_read = may_read or (lambda reader, kept: False)
        self.rows: dict[str, Kept] = {}

    def save(self, kind: str, payload: dict, *, owner: str, name: str, description: str = "") -> dict:
        """Keep `payload` for `owner`; the MCP `resource_link` a tool returns for it."""
        rid = secrets.token_urlsafe(18)
        self.rows[rid] = Kept(owner=owner, kind=kind, payload=payload)
        return resources.resource_link(f"{self.scheme}://{rid}", name, description=description)

    def read(self, uri: str, reader: str | None) -> Kept:
        parts = urlsplit(uri)
        kept = self.rows.get(parts.netloc) if parts.scheme == self.scheme else None
        if kept is None:
            raise Refused("not_found: no such result")
        if reader != kept.owner and not self.may_read(reader, kept):
            raise Refused("forbidden: not yours to read")
        return kept

    def contents(self, uri: str, reader: str | None) -> list[dict]:
        """The `resources/read` answer: one JSON text content, a page of a table's rows."""
        kept = self.read(uri, reader)
        payload = dict(kept.payload)
        query = parse_qs(urlsplit(uri).query)
        if "rows" in payload and "page_size" in query:
            size = max(1, int(query["page_size"][0]))
            page = max(1, int(query.get("page", ["1"])[0]))
            payload["rows"] = payload["rows"][(page - 1) * size:page * size]
        return [{"uri": uri, "mimeType": kept.mime_type, "text": json.dumps(payload),
                 "_meta": {META_KEY: {"kind": kept.kind}}}]

    def mount(self, server, reader: Callable[[], str | None]) -> None:
        """`resources/read` for `<scheme>://{id}{?page,page_size}` on a FastMCP server,
        answering as `reader()`."""
        from fastmcp.exceptions import ResourceError
        from fastmcp.resources import ResourceContent, ResourceResult

        async def result(rid: str, page: str = "", page_size: str = ""):
            query = "&".join(f"{k}={v}" for k, v in (("page", page), ("page_size", page_size)) if v)
            uri = f"{self.scheme}://{rid}" + (f"?{query}" if query else "")
            try:
                contents = self.contents(uri, reader())
            except Refused as exc:
                raise ResourceError(str(exc)) from None
            return ResourceResult(contents=[ResourceContent(content=c["text"], mime_type=c["mimeType"],
                                                            meta=c["_meta"]) for c in contents])

        server.resource(f"{self.scheme}://{{rid}}{{?page,page_size}}", name="results",
                        mime_type="application/json")(result)


def facts(columns: list, rows: list, sample: int = 1) -> dict:
    """What the model gets instead of the table."""
    return {"row_count": len(rows), "columns": list(columns), "sample": [list(r) for r in rows[:sample]]}
