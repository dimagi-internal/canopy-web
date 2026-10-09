"""MCP Apps (SEP-1865, `io.modelcontextprotocol/ui`) — canopy as a HOST.

A Connected site's MCP server may attach a View (a `ui://` HTML resource) to a
tool. canopy renders that View sandboxed in its chat, and the View's own calls go
back to the site AS WHOEVER IS LOOKING AT IT. Design and owner decisions:
`docs/superpowers/specs/2026-10-08-mcp-apps-host-design.md`. The standard is
`specification/2026-01-26/apps.mdx` in `modelcontextprotocol/ext-apps`.

This module is the pure half — no network, no request:

* **Tool metadata.** Only `_meta.ui` counts (`resourceUri`, `visibility`). The
  deprecated flat `_meta["ui/resourceUri"]` is deliberately NOT read: the spec
  removes it before GA, and reading it would make a host that only sets the old
  key look supported until the day it silently is not.
* **The UI tool index.** Per Connected site, which tools carry a View and who may
  call them (`AppCredential.mcp_apps_index`). Refreshed whenever canopy lists the
  host's tools as somebody (the gateway, a View call, a visitor's arrival). It is
  a CACHE for deciding which transcript rows get a View — never an authority:
  every View call re-lists as the viewer and checks visibility then.
* **CSP.** From the resource's `_meta.ui.csp`, narrowed to the site's own
  registered origins (the spec lets a host restrict further, never loosen), with
  the spec's restrictive default when it is omitted.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from django.utils import timezone

#: The extension identifier canopy advertises in `initialize` and a host declares.
UI_EXTENSION = "io.modelcontextprotocol/ui"
#: The only View content type the 2026-01-26 spec defines.
RESOURCE_MIME = "text/html;profile=mcp-app"
#: What canopy answers a View's `ui/initialize` with.
PROTOCOL_VERSION = "2026-01-26"
#: `_meta.ui.visibility` when a tool omits it (§Visibility).
DEFAULT_VISIBILITY = ("model", "app")
MODEL, APP = "model", "app"

#: An index entry not seen in a listing for this long is dropped: a host that
#: retired a View should stop getting one rendered for it.
INDEX_TTL = timedelta(days=7)
#: Biggest View HTML canopy will relay or cache. Labs' View is one document with
#: inline JS and CSS; a megabyte is far beyond that and far below a DB row's pain.
MAX_RESOURCE_BYTES = 1024 * 1024

CSP_KEYS = ("connectDomains", "resourceDomains", "frameDomains", "baseUriDomains")


# --- tool metadata --------------------------------------------------------------


def ui_meta(meta) -> dict:
    """`{"resource_uri", "visibility"}` from a tool's `_meta`, or {} if it has no UI.

    `resource_uri` is "" for an app-only helper with no View of its own; the
    visibility is still recorded, because that is what hides it from the agent.
    """
    if not isinstance(meta, dict):
        return {}
    ui = meta.get("ui")
    if not isinstance(ui, dict):
        return {}
    uri = ui.get("resourceUri")
    uri = uri if isinstance(uri, str) and uri.startswith("ui://") else ""
    return {"resource_uri": uri, "visibility": list(visibility_of(meta))}


def visibility_of(meta) -> tuple[str, ...]:
    """The tool's visibility, defaulted as the spec says. An unreadable value is
    treated as the default rather than as "nothing": a malformed list must not
    make a model tool vanish."""
    ui = meta.get("ui") if isinstance(meta, dict) else None
    raw = ui.get("visibility") if isinstance(ui, dict) else None
    if not isinstance(raw, list):
        return DEFAULT_VISIBILITY
    values = tuple(v for v in raw if v in (MODEL, APP))
    return values if values or raw == [] else DEFAULT_VISIBILITY


def model_visible(meta) -> bool:
    """§Visibility: the host MUST NOT list a tool to the agent without "model"."""
    return MODEL in visibility_of(meta)


def app_visible(meta) -> bool:
    """§Visibility: the host MUST reject a View's call to a tool without "app"."""
    return APP in visibility_of(meta)


# --- the UI tool index -----------------------------------------------------------


def _now_iso() -> str:
    return timezone.now().isoformat()


def record_tools(app_id: int, tools: list[tuple[str, dict]]) -> None:
    """Fold one listing into the site's index. `tools` is `(name, _meta)` pairs.

    A UNION across listings, refreshed per tool: two visitors with different
    scopes see different subsets, and neither listing is the whole server. A
    tool the listing shows with no `_meta.ui` is removed (the host took its View
    away); one not in this listing keeps its entry until `INDEX_TTL` passes.
    """
    from django.db import transaction

    from .models import AppCredential

    now = timezone.now()
    with transaction.atomic():
        app = AppCredential.objects.select_for_update().filter(pk=app_id).first()
        if app is None:
            return
        index = dict(app.mcp_apps_index or {})
        entries = dict(index.get("tools") or {})
        for name, meta in tools:
            ui = ui_meta(meta)
            if ui:
                entries[name] = {**ui, "seen_at": now.isoformat()}
            else:
                entries.pop(name, None)
        cutoff = now - INDEX_TTL
        entries = {n: e for n, e in entries.items() if _parse(e.get("seen_at")) >= cutoff}
        index["tools"] = entries
        index["refreshed_at"] = now.isoformat()
        if index != (app.mcp_apps_index or {}):
            app.mcp_apps_index = index
            app.save(update_fields=["mcp_apps_index"])


def _parse(value):
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return timezone.now() - INDEX_TTL - timedelta(days=1)


def indexed_tool(app, name: str) -> dict | None:
    """The index entry for a host tool name, or None."""
    entry = ((app.mcp_apps_index or {}).get("tools") or {}).get(name)
    return entry if isinstance(entry, dict) else None


def index_age(app) -> timedelta | None:
    refreshed = (app.mcp_apps_index or {}).get("refreshed_at")
    return (timezone.now() - _parse(refreshed)) if refreshed else None


def record_resource(app_id: int, uri: str, *, html: str, meta: dict) -> str:
    """Cache a fetched View and log its hash + effective CSP once per change
    (§Security 3, "Generate hash/signature for resources"). Returns the sha256.

    The cache exists for one reader: a viewer with no live grant, who still gets
    the View read-only (owner decision 4) — the View itself is the host's code,
    not the viewer's data, so showing a copy fetched under someone else's grant
    leaks nothing that viewer could not already see in the transcript.
    """
    import logging

    from django.db import transaction

    from .models import AppCredential

    sha = hashlib.sha256(html.encode("utf-8")).hexdigest()
    with transaction.atomic():
        app = AppCredential.objects.select_for_update().filter(pk=app_id).first()
        if app is None:
            return sha
        index = dict(app.mcp_apps_index or {})
        resources = dict(index.get("resources") or {})
        previous = resources.get(uri) or {}
        effective, dropped = effective_csp(_declared_csp(meta), site_origins(app))
        if previous.get("sha256") != sha or previous.get("csp") != effective:
            logging.getLogger(__name__).info(
                "mcp_apps.resource site=%s uri=%s sha256=%s csp=%s dropped=%s",
                app.name, uri, sha, json.dumps(effective, sort_keys=True), dropped)
        resources[uri] = {
            "sha256": sha, "csp": effective, "dropped": dropped,
            "prefers_border": _prefers_border(meta),
            "html": html, "fetched_at": _now_iso(),
            "changed_at": _now_iso() if previous.get("sha256") != sha else previous.get("changed_at"),
        }
        index["resources"] = resources
        app.mcp_apps_index = index
        app.save(update_fields=["mcp_apps_index"])
    return sha


def cached_resource(app, uri: str) -> dict | None:
    entry = ((app.mcp_apps_index or {}).get("resources") or {}).get(uri)
    return entry if isinstance(entry, dict) and entry.get("html") else None


def _declared_csp(meta) -> dict:
    ui = meta.get("ui") if isinstance(meta, dict) else None
    csp = ui.get("csp") if isinstance(ui, dict) else None
    return csp if isinstance(csp, dict) else {}


def _prefers_border(meta):
    ui = meta.get("ui") if isinstance(meta, dict) else None
    value = ui.get("prefersBorder") if isinstance(ui, dict) else None
    return value if isinstance(value, bool) else None


# --- CSP --------------------------------------------------------------------------

#: A CSP source canopy will put in a header: scheme, host (optionally `*.`-led)
#: and port. No path, no query, no quotes, no whitespace — anything else could
#: smuggle a directive (`; script-src *`) or a keyword (`'unsafe-eval'`).
_SOURCE = re.compile(r"^(https|wss)://(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(:\d{1,5})?$")


def normalize_source(value) -> str | None:
    """A declared domain as a safe CSP source, or None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower().rstrip("/")
    return v if _SOURCE.match(v) else None


def _origin(url: str) -> str | None:
    parts = urlsplit(url or "")
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def site_origins(app) -> set[str]:
    """The origins a Connected site has REGISTERED with canopy: the pages that may
    frame its embed, its MCP server, its grant issuer. The only domains a View
    from that site may name in its CSP — anything else is dropped (and logged),
    however the resource declares it."""
    origins = set(app.frame_origins())
    for url in (app.host_mcp_resource, app.host_issuer):
        o = _origin(url)
        if o:
            origins.add(o)
    return {o for o in origins if o.startswith("https://")}


def effective_csp(declared: dict, allowed: set[str]) -> tuple[dict, list[str]]:
    """`declared` narrowed to `allowed` origins. Returns (csp, dropped).

    Never widens: an omitted or empty list stays empty (which the header turns
    into the restrictive default). A `wss://` source is honoured for an allowed
    `https://` origin of the same host — a WebSocket to the site's own server is
    still the site's own server. Wildcards are never honoured: a registered
    origin is one host.
    """
    out: dict[str, list[str]] = {}
    dropped: list[str] = []
    for key in CSP_KEYS:
        values = declared.get(key) if isinstance(declared, dict) else None
        kept: list[str] = []
        for raw in values if isinstance(values, list) else []:
            src = normalize_source(raw)
            https = ("https://" + src[len("wss://"):]) if src and src.startswith("wss://") else src
            if src and "*" not in src and https in allowed:
                if src not in kept:
                    kept.append(src)
            else:
                dropped.append(f"{key}:{str(raw)[:120]}")
        if kept:
            out[key] = kept
    return out, dropped


def csp_header(csp: dict, *, frame_ancestors: list[str]) -> str:
    """The Content-Security-Policy for the sandbox proxy, and so for the View.

    The View is the proxy's `srcdoc` child, which INHERITS the proxy's policy
    container — one policy covers both. The spec's restrictive default
    (§UI Resource Format, "Host Behavior") with two narrowings: no `'self'`
    (the proxy's origin is opaque, so `'self'` could only ever mean canopy's
    own URLs, which a View has no business loading) and `form-action 'none'`.
    `sandbox allow-scripts` makes the document opaque even when opened top-level,
    the same belt the walkthrough content route wears.
    """
    def srcs(key):
        return " ".join(s for s in (normalize_source(v) for v in csp.get(key) or []) if s)

    def or_none(value):
        return value or "'none'"

    resources = srcs("resourceDomains")
    directives = [
        "default-src 'none'",
        f"script-src 'unsafe-inline' {resources}".strip(),
        f"style-src 'unsafe-inline' {resources}".strip(),
        f"img-src data: {resources}".strip(),
        f"font-src {or_none(resources)}",
        f"media-src data: {resources}".strip(),
        f"connect-src {or_none(srcs('connectDomains'))}",
        f"frame-src {or_none(srcs('frameDomains'))}",
        "object-src 'none'",
        f"base-uri {or_none(srcs('baseUriDomains'))}",
        "form-action 'none'",
        f"frame-ancestors {or_none(' '.join(frame_ancestors))}",
        "sandbox allow-scripts",
    ]
    return "; ".join(directives)


def encode_csp(csp: dict) -> str:
    raw = json.dumps({k: csp[k] for k in CSP_KEYS if csp.get(k)}, separators=(",", ":"),
                     sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_csp(value: str) -> dict:
    """The `?csp=` query of a sandbox URL, re-validated. Anything unreadable is
    the empty (restrictive) policy, never an error page a View could probe."""
    if not value or len(value) > 4096:
        return {}
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        data = json.loads(raw)
    except (binascii.Error, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in CSP_KEYS:
        raw_values = data.get(key)
        values = [s for s in (normalize_source(v) for v in
                              (raw_values if isinstance(raw_values, list) else [])) if s]
        if values:
            out[key] = values[:32]
    return out


def sandbox_src(csp: dict) -> str:
    """Where the host frames the proxy for a View with this (effective) CSP.

    `MCP_APPS_SANDBOX_URL` (a canopy sub-path by default, owner decision 1) is a
    setting so moving the proxy to a dedicated origin later is config only.
    """
    from django.conf import settings

    base = settings.MCP_APPS_SANDBOX_URL
    query = urlencode({"csp": encode_csp(csp)}) if any(csp.get(k) for k in CSP_KEYS) else ""
    if not query:
        return base
    return f"{base}{'&' if '?' in base else '?'}{query}"
