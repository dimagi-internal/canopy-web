"""Host-held results: a tool returns facts + an artifact, in a line
(spec ``2026-10-23-host-held-results-design``).

The host owns what its tools produce; canopy carries the conversation and the
wiring, never the data. So a tool that produced rows returns FACTS for the model
(``summarize_rows``) and an MCP ``resource_link`` naming an artifact the host keeps;
the person's View and the agent read it back from the host, as themselves, with
``resources/read``::

    from canopy_sdk.host.artifacts import Artifacts, MemoryArtifactStore, summarize_rows

    artifacts = Artifacts(MemoryArtifactStore(), scheme="labs-result")

    @mcp.tool
    def data_query(sql: str):
        columns, rows = run(sql)
        link = artifacts.save(owner=principal.subject, kind="query",
                              payload={"columns": columns, "rows": rows},
                              tool_call_id=request_id, name="Query result")
        return [TextContent(type="text", text=json.dumps(summarize_rows(columns, rows))), link]

    # resources/read on labs-result://… — as the reader:
    artifacts.contents(uri, reader=principal.subject)

* **Owner.** An artifact belongs to the subject the call ran as: on a delegated MCP
  request that is ``DelegatedPrincipal.subject`` (the visitor's id as the host
  knows it — the ID-JAG's ``sub``; ``current_subject()`` reads it when the host's
  ``DPoPGate`` resolves the principal).
* **Who may read.** The owner; anyone the host's own ACL would let see the
  underlying data (``may_read(reader, artifact)``, e.g. a colleague on the same
  conversation); nobody else. Each refusal is named (``ArtifactRefused.code``).
* **Expiry.** ``ttl`` seconds (default a day, the host's choice); an expired
  artifact reads as ``expired`` and ``sweep()`` deletes it.
* **Snapshot or fresh.** Served as stored (what the agent saw) unless the host
  passes ``on_read(artifact, reader)``, whose non-None answer is served instead;
  the content's ``_meta`` says which (``snapshot``).
* **Paging.** A table payload (``{"columns": [...], "rows": [...]}``) pages through
  the URI: ``<scheme>://<id>?page=2&page_size=100`` — never through the chat.

Framework-free: ``MemoryArtifactStore`` for tests and small hosts, any object with
the ``ArtifactStore`` methods for the rest; ``canopy_sdk.django.artifacts`` is the
Django model, sweep, resource handler and settings.
"""
from __future__ import annotations

import json
import math
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from ..resources import ResourceError, check_scheme, resource_link

#: How long an artifact lives unless the host says otherwise.
DEFAULT_TTL = 24 * 60 * 60
#: Rows per page of a table artifact, unless the reader asks for another size.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
#: The ``_meta`` key a read's content carries about the artifact.
META_KEY = "canopy/artifact"
_ID_BYTES = 18


class ArtifactRefused(Exception):
    """Why a read is refused. ``code``: ``bad_uri`` (not one of this host's
    artifact URIs), ``not_found``, ``expired`` or ``forbidden`` (the reader is not
    the owner and the host's ACL does not let them see it)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Artifact:
    """One thing a tool produced, kept by the host for the person it ran as."""

    id: str
    scheme: str
    owner: str
    kind: str
    payload: Any
    tool_call_id: str = ""
    name: str = ""
    description: str = ""
    mime_type: str = "application/json"
    created_at: float = 0.0
    expires_at: float = 0.0
    #: Recomputed on this read (``on_read``), not the stored snapshot.
    fresh: bool = False

    @property
    def uri(self) -> str:
        return f"{self.scheme}://{self.id}"

    def expired(self, now: float) -> bool:
        return self.expires_at <= now

    def link(self) -> dict:
        """The MCP ``resource_link`` naming it."""
        return resource_link(self.uri, self.name or self.kind or "result", mime_type=self.mime_type,
                             description=self.description)


class ArtifactStore(Protocol):
    """Where a host keeps artifacts. ``canopy_sdk.django.artifacts.DjangoArtifactStore``
    is the Django one; ``MemoryArtifactStore`` the in-process one."""

    def put(self, artifact: Artifact) -> None: ...
    def get(self, scheme: str, artifact_id: str) -> Artifact | None: ...
    def set_payload(self, scheme: str, artifact_id: str, payload: Any) -> bool: ...
    def delete_expired(self, now: float) -> int: ...


class MemoryArtifactStore:
    """An in-process store: tests, a single-process host, a prototype."""

    def __init__(self):
        self._rows: dict[tuple[str, str], Artifact] = {}
        self._lock = threading.Lock()

    def put(self, artifact: Artifact) -> None:
        with self._lock:
            self._rows[(artifact.scheme, artifact.id)] = artifact

    def get(self, scheme: str, artifact_id: str) -> Artifact | None:
        return self._rows.get((scheme, artifact_id))

    def set_payload(self, scheme: str, artifact_id: str, payload: Any) -> bool:
        with self._lock:
            row = self._rows.get((scheme, artifact_id))
            if row is None:
                return False
            self._rows[(scheme, artifact_id)] = replace(row, payload=payload)
            return True

    def delete_expired(self, now: float) -> int:
        with self._lock:
            gone = [k for k, a in self._rows.items() if a.expired(now)]
            for k in gone:
                del self._rows[k]
            return len(gone)


def current_subject() -> str | None:
    """The subject a delegated MCP request acts for, when the host's ``DPoPGate``
    resolved it (``require_principal=True``): the owner of anything the call saves,
    and the reader of anything it reads. None outside such a request."""
    from .asgi import delegated_principal

    principal = delegated_principal.get()
    return principal.subject if principal is not None else None


def _as_json(payload: Any) -> Any:
    """``payload`` as JSON will hold it (a date as its ISO string, a tuple as a
    list): what is stored is exactly what every reader will get back."""
    return json.loads(json.dumps(payload, default=str))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class Artifacts:
    """Save, read, page and expire one host's artifacts.

    ``scheme`` is the URI scheme the host's artifacts live under (declare it in
    ``site.json``'s ``site.resource_schemes`` — the Django host does — or canopy drops
    the links); ``extra_schemes`` are others it also serves. ``may_read(reader,
    artifact) -> bool`` is the host's own ACL for a reader who is not the owner.
    ``on_read(artifact, reader)`` recomputes on read (its non-None answer is served
    as fresh). ``notify(uris)`` is called after ``update`` — the Django host posts it
    to canopy so subscribed Views re-read."""

    def __init__(self, store: ArtifactStore, *, scheme: str, ttl: int = DEFAULT_TTL,
                 extra_schemes: Iterable[str] = (), mime_type: str = "application/json",
                 may_read: Callable[[str | None, Artifact], bool] | None = None,
                 on_read: Callable[[Artifact, str | None], Any] | None = None,
                 notify: Callable[[list[str]], Any] | None = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self.scheme = check_scheme(scheme)
        self.schemes = frozenset([self.scheme, *(check_scheme(s) for s in extra_schemes)])
        self.ttl = int(ttl)
        if self.ttl <= 0:
            raise ValueError("an artifact's ttl must be positive")
        self.mime_type = mime_type
        self.may_read = may_read
        self.on_read = on_read
        self.notify = notify
        self.clock = clock

    # --- writing --------------------------------------------------------------------

    def save(self, owner: str, kind: str, payload: Any, *, tool_call_id: str = "",
             ttl: int | None = None, scheme: str | None = None, name: str = "",
             description: str = "", mime_type: str | None = None) -> dict:
        """Keep ``payload`` for ``owner`` and return the MCP ``resource_link`` naming
        it — put it in the tool's result content beside the facts."""
        owner = str(owner or "").strip()
        if not owner:
            raise ValueError("an artifact needs its owner: the subject the call ran as")
        scheme = check_scheme(scheme) if scheme else self.scheme
        if scheme not in self.schemes:
            raise ValueError(f"{scheme!r} is not one of this host's artifact schemes")
        payload = _as_json(payload)
        now = self.clock()
        artifact = Artifact(id=secrets.token_urlsafe(_ID_BYTES), scheme=scheme, owner=owner,
                            kind=str(kind or ""), payload=payload, tool_call_id=str(tool_call_id or ""),
                            name=str(name or kind or "result")[:200], description=str(description or "")[:500],
                            mime_type=mime_type or self.mime_type, created_at=now,
                            expires_at=now + (int(ttl) if ttl else self.ttl))
        link = artifact.link()
        self.store.put(artifact)
        return link

    def update(self, uri: str, payload: Any) -> bool:
        """Replace an artifact's payload (a draft edited, a result recomputed) and
        tell canopy (``notify``) so subscribed Views re-read. False when unknown."""
        scheme, artifact_id, _ = self.parse(uri)
        payload = _as_json(payload)
        changed = self.store.set_payload(scheme, artifact_id, payload)
        if changed and self.notify is not None:
            self.notify([f"{scheme}://{artifact_id}"])
        return changed

    def sweep(self) -> int:
        """Delete every expired artifact; how many went."""
        return self.store.delete_expired(self.clock())

    # --- reading --------------------------------------------------------------------

    def parse(self, uri: str) -> tuple[str, str, dict[str, str]]:
        """``(scheme, id, query)`` of one of this host's artifact URIs, or
        ``ArtifactRefused("bad_uri")``."""
        parts = urlsplit(str(uri or ""))
        scheme = parts.scheme.lower()
        artifact_id = parts.netloc
        if scheme not in self.schemes or not artifact_id or parts.path not in ("", "/"):
            raise ArtifactRefused("bad_uri", f"{str(uri)[:100]!r} is not one of this host's results")
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        return scheme, artifact_id, query

    def read(self, uri: str, reader: str | None) -> Artifact:
        """The artifact ``uri`` names, as ``reader`` may see it — or a named refusal."""
        scheme, artifact_id, _ = self.parse(uri)
        artifact = self.store.get(scheme, artifact_id)
        if artifact is None:
            raise ArtifactRefused("not_found", "there is no such result here")
        if artifact.expired(self.clock()):
            raise ArtifactRefused("expired", "this result has expired; run the query again")
        if not self._may(reader, artifact):
            raise ArtifactRefused("forbidden", "this result is not yours to read")
        if self.on_read is not None:
            fresh = self.on_read(artifact, reader)
            if fresh is not None:
                return replace(artifact, payload=fresh, fresh=True)
        return artifact

    def _may(self, reader: str | None, artifact: Artifact) -> bool:
        if reader is not None and str(reader) == artifact.owner:
            return True
        if self.may_read is None:
            return False
        try:
            return bool(self.may_read(reader, artifact))
        except Exception:  # noqa: BLE001 - an ACL that cannot answer says no
            return False

    def contents(self, uri: str, reader: str | None) -> list[dict]:
        """The MCP ``resources/read`` answer (``contents``) for ``uri``, read as
        ``reader``: one JSON text content with ``_meta["canopy/artifact"]`` saying
        what it is, and a page of the rows when the payload is a table."""
        _, _, query = self.parse(uri)
        artifact = self.read(uri, reader)
        body, paging = page(artifact.payload, query)
        meta = {"kind": artifact.kind, "tool_call_id": artifact.tool_call_id,
                "created_at": _iso(artifact.created_at), "expires_at": _iso(artifact.expires_at),
                "snapshot": not artifact.fresh, **paging}
        text = body if isinstance(body, str) else json.dumps(body, default=str)
        return [{"uri": uri, "mimeType": artifact.mime_type, "text": text, "_meta": {META_KEY: meta}}]

    def template(self, *, name: str = "results", description: str = "") -> dict:
        """This host's ``resources/templates/list`` entry for its artifacts."""
        return {"uriTemplate": f"{self.scheme}://{{id}}{{?page,page_size}}", "name": name,
                "mimeType": self.mime_type,
                "description": description or "A result a tool produced, kept for the person it ran as."}


def page(payload: Any, query: Mapping[str, str]) -> tuple[Any, dict]:
    """``(body, paging meta)``: a table payload's rows sliced to the page the
    query asks for; anything else whole."""
    if not (isinstance(payload, Mapping) and isinstance(payload.get("rows"), list)):
        return payload, {}
    rows = payload["rows"]

    def number(key: str, default: int) -> int:
        try:
            return int(query.get(key) or default)
        except (TypeError, ValueError):
            return default

    size = max(1, min(MAX_PAGE_SIZE, number("page_size", DEFAULT_PAGE_SIZE)))
    pages = max(1, math.ceil(len(rows) / size))
    current = max(1, min(pages, number("page", 1)))
    body = {**payload, "rows": rows[(current - 1) * size: current * size]}
    return body, {"page": current, "page_size": size, "pages": pages, "row_count": len(rows)}


# --- facts ---------------------------------------------------------------------------


def _column_names(columns: Sequence) -> list[str]:
    out = []
    for c in columns or ():
        if isinstance(c, Mapping):
            out.append(str(c.get("name") or c.get("key") or c.get("id") or ""))
        else:
            out.append(str(c))
    return out


def summarize_rows(columns: Sequence, rows: Sequence, sample: int = 5) -> dict:
    """The facts a model gets about a table instead of the table: how many rows,
    the columns, and the first ``sample`` rows (``sample=0`` for none — in a
    sensitive conversation the model still sees what a sample shows).

    ``columns`` are names or ``{"name": ...}`` mappings; ``rows`` are sequences in
    column order or mappings by name."""
    names = _column_names(columns)
    rows = list(rows or ())
    take = max(0, int(sample))

    def as_dict(row) -> dict:
        if isinstance(row, Mapping):
            return {n: row.get(n) for n in names} if names else dict(row)
        return dict(zip(names, row))

    return {"row_count": len(rows), "columns": names,
            "sample_rows": [as_dict(r) for r in rows[:take]],
            "truncated": len(rows) > take}


# --- one store per process, for hosts that want module-level calls --------------------

_default: Artifacts | None = None


def configure(artifacts: Artifacts | None) -> None:
    """Make ``artifacts`` what the module-level ``save`` / ``read`` / ``contents`` /
    ``sweep`` use (None clears it). The Django host configures nothing here: it
    reads ``CANOPY_HOST["ARTIFACTS"]`` on every call (``canopy_sdk.django.artifacts``)."""
    global _default
    _default = artifacts


def default() -> Artifacts:
    if _default is None:
        raise ResourceError("not_configured", "no artifact store is configured "
                                              "(canopy_sdk.host.artifacts.configure)")
    return _default


def save(owner: str, kind: str, payload: Any, *, tool_call_id: str = "", ttl: int | None = None,
         scheme: str | None = None, **kw) -> dict:
    return default().save(owner, kind, payload, tool_call_id=tool_call_id, ttl=ttl, scheme=scheme, **kw)


def read(uri: str, reader: str | None) -> Artifact:
    return default().read(uri, reader)


def contents(uri: str, reader: str | None) -> list[dict]:
    return default().contents(uri, reader)


def sweep() -> int:
    return default().sweep()
