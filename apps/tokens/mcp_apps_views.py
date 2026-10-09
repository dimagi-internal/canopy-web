"""MCP Apps: which transcript rows get a View, and what a View may do — as the VIEWER.

Spec 2026-10-08 §3-6 (owner decisions in its last section). The browser routes
that serve a rendered View (`canopy_sessions/app_views_api.py` for canopy users,
`tokens/contact_api.py` for contacts) are thin; this module is the whole rule.

**Which rows (§3).** A `tool_result` gets a View when (a) the session was held on
a Connected site (`metadata.embed_app`) and (b) that site's UI tool index has a
`resourceUri` for the host tool the call named — either path:

* **G** — the agent called `site_call`; the host tool is `input.tool`;
* **D** — the agent called the site on its own MCP login; the host tool is the
  last segment of the tool's name (`host_gateway.host_tool_name`).

canopy decides this server-side and says so on the row (`app`); a client never
guesses. The id is the tool call's OWN correlation id (`tool_use.id`, carried by
the result as `tool_use_id`) — the one id a live frame and a reload agree on.

**Who acts (§4).** A View's call runs under the grant of the person who CLICKED
(`resolve_for_viewer`), never the turn's initiator's and never the agent's. Gates,
each a refusal with its own audit code (`GATES`): may write here → a real View row
→ same server → `"app"` visibility → the owner's ceiling → a live grant → the
host's own ACL.

**What the agent learns (§6).** canopy records, itself, each View call to a tool
the model could also call (a commit): a RECEIPT, shown under the View and handed
to the next turn in the caller envelope (`envelope_block`). A View's
`ui/update-model-context` is stored per View, latest wins, 8 KiB, and rides the
same envelope. `ui/message` is a user message authored by the viewer — a turn.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import threading
from dataclasses import dataclass

from asgiref.sync import async_to_sync
from django.db import transaction
from django.utils import timezone

from . import host_gateway, mcp_apps

log = logging.getLogger(__name__)

#: The metadata key on a Session this module owns (server-owned: a host can
#: never write it through session metadata).
META_KEY = "mcp_apps"
MAX_CONTEXT_BYTES = 8192
MAX_CONTEXTS = 10
MAX_RECEIPTS = 30
MAX_MESSAGE_CHARS = 4000
RESULT_EXCERPT = 600

#: Audit codes, one per gate (spec §4's table) plus the host's own outcome.
GATES = {
    1: "viewer_cannot_act",
    2: "not_a_view",
    3: "not_on_server",
    4: "not_app_visible",
    5: "not_allowed",
    6: "no_grant",
}


class ViewRefusal(Exception):
    """A View request canopy will not carry out. `status` is the HTTP answer."""

    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Viewer:
    """Whoever is looking at the View: a canopy user or a contact, never both."""

    user: object | None = None
    contact: object | None = None

    @property
    def name(self) -> str:
        if self.contact is not None:
            return self.contact.display_name or "a visitor"
        u = self.user
        full = (u.get_full_name() or "").strip() if u is not None else ""
        return full or getattr(u, "email", "") or getattr(u, "username", "") or "someone"

    def ref(self) -> dict:
        if self.contact is not None:
            return {"name": self.name, "contact_id": self.contact.pk}
        return {"name": self.name, "user_id": getattr(self.user, "pk", None)}

    def label(self) -> str:
        if self.contact is not None:
            return f"contact:{self.contact.pk}"
        return f"user:{getattr(self.user, 'pk', None)}"

    def may_act(self, session) -> bool:
        """Gate 1: owner/editor of the session; a contact in their own session."""
        if self.contact is not None:
            return session.contact_id == self.contact.pk
        from apps.canopy_sessions import access

        return access.can_write(self.user, session)


# --- the site, and which rows have a View ------------------------------------------------


def site_app(session):
    """The session's live Connected site, in the AGENT's tenant, or None."""
    from .models import AppCredential

    site = str((session.metadata or {}).get("embed_app") or "").strip()
    if not site:
        return None
    ws = session.agent.workspace_id if session.agent_id else session.workspace_id
    return AppCredential.objects.filter(name=site, workspace_id=ws,
                                        revoked_at__isnull=True).first()


def host_tool_of(use: dict) -> tuple[str, str, dict]:
    """(host tool, path "gateway"|"direct", the host tool's arguments) for a
    `tool_use` block's content."""
    name = str(use.get("name") or "")
    raw = use.get("input")
    args = raw if isinstance(raw, dict) else {}
    if host_gateway.host_tool_name(name) == "site_call":
        inner = args.get("arguments")
        return (host_gateway.host_tool_name(str(args.get("tool") or "")), "gateway",
                inner if isinstance(inner, dict) else {})
    return host_gateway.host_tool_name(name), "direct", args


def app_ref(app, use: dict) -> dict | None:
    """The `app` a row carries when its tool call has a View, else None."""
    call_id = use.get("id")
    if not isinstance(call_id, str) or not call_id:
        return None
    tool, path, _args = host_tool_of(use)
    entry = mcp_apps.indexed_tool(app, tool) if tool else None
    if not entry or not entry.get("resource_uri"):
        return None
    return {"tool_call_id": call_id, "site": app.name, "tool": tool,
            "resource_uri": entry["resource_uri"], "path": path}


def annotate(session, messages) -> None:
    """Set `.app` on every tool_result row of `messages` whose call has a View.

    In memory only. Cheap when there is nothing to do (no site, no indexed View),
    which is every session not held on a Connected site.
    """
    from apps.canopy_sessions.models import Message

    results = [m for m in messages if getattr(m, "role", None) == Message.TOOL_RESULT]
    if not results:
        return
    app = site_app(session)
    if app is None or not (app.mcp_apps_index or {}).get("tools"):
        return
    uses = {}
    for m in messages:
        content = getattr(m, "content", None)
        if m.role == Message.TOOL_USE and isinstance(content, dict) and content.get("id"):
            uses[content["id"]] = content
    wanted = [str((getattr(m, "content", None) or {}).get("tool_use_id") or "") for m in results]
    missing = [w for w in wanted if w and w not in uses]
    if missing:
        for content in (Message.objects.filter(session=session, role=Message.TOOL_USE,
                                               content__id__in=missing)
                        .values_list("content", flat=True)):
            uses[content.get("id")] = content
    for m, call_id in zip(results, wanted):
        use = uses.get(call_id)
        ref = app_ref(app, use) if use else None
        if ref:
            m.app = ref


def app_for_result_block(session, block: dict) -> dict | None:
    """The live-frame form of `annotate`: one tool_result block arriving now."""
    from apps.canopy_sessions.models import Message

    call_id = str((block or {}).get("tool_use_id") or "")
    if not call_id:
        return None
    app = site_app(session)
    if app is None or not (app.mcp_apps_index or {}).get("tools"):
        return None
    use = (Message.objects.filter(session=session, role=Message.TOOL_USE, content__id=call_id)
           .values_list("content", flat=True).first())
    return app_ref(app, use) if use else None


def strip_for_widget(message):
    """A View row as an embedded widget receives it: the `app` and the call id,
    not the tool's raw result (the View fetches what it needs as the viewer)."""
    import copy

    out = copy.copy(message)
    out.content = {"tool_use_id": (message.content or {}).get("tool_use_id")}
    out.plaintext = ""
    return out


# --- one View, looked up --------------------------------------------------------------


@dataclass
class ViewRow:
    app: object
    ref: dict
    use: dict
    result: dict | None


def find(session, tool_call_id: str) -> ViewRow:
    """Gate 2: `tool_call_id` names a View-bearing call in THIS session."""
    from apps.canopy_sessions.models import Message

    app = site_app(session)
    use = (Message.objects.filter(session=session, role=Message.TOOL_USE,
                                  content__id=tool_call_id)
           .values_list("content", flat=True).first()) if app is not None else None
    ref = app_ref(app, use) if use else None
    result = (Message.objects.filter(session=session, role=Message.TOOL_RESULT,
                                     content__tool_use_id=tool_call_id)
              .values_list("content", flat=True).first()) if ref else None
    if ref is None or result is None:
        raise ViewRefusal(GATES[2], "there is no app view for that tool call here", 404)
    return ViewRow(app=app, ref=ref, use=use, result=result)


def tool_input(row: ViewRow) -> dict:
    return host_tool_of(row.use)[2]


def tool_result(row: ViewRow) -> dict | None:
    """The call's result as a `CallToolResult`, as best the transcript holds it.

    Path G's row is `site_call`'s own answer (`{content, structured, is_error}`),
    so `structuredContent` survives; path D's is whatever the runner recorded,
    usually text only — which is why the Labs View needs only the arguments.
    """
    if row.result is None:
        return None
    body = row.result.get("content")
    is_error = bool(row.result.get("is_error"))
    blocks = _blocks(body)
    if row.ref["path"] == "gateway":
        for b in blocks:
            try:
                data = json.loads(b.get("text") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and "content" in data:
                out = {"content": data.get("content") or [],
                       "isError": bool(data.get("is_error"))}
                if data.get("structured") is not None:
                    out["structuredContent"] = data["structured"]
                return out
    return {"content": blocks, "isError": is_error}


def _blocks(body) -> list[dict]:
    if isinstance(body, str):
        return [{"type": "text", "text": body}]
    if isinstance(body, list):
        return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in body]
    return []


# --- the viewer's grant -------------------------------------------------------------------


def _ceiling(session, row: ViewRow) -> list[str]:
    """The owner's ceiling for this site, if the conversation has one. The
    capability of the turn that made the call; none known → none (the host's
    grant decides, as it does for every call)."""
    from apps.canopy_sessions.models import Message

    agent = session.agent if session.agent_id else None
    if agent is None:
        return []
    turn = (Message.objects.filter(session=session, role=Message.TOOL_USE,
                                   content__id=row.ref["tool_call_id"])
            .select_related("turn").values_list("turn__capability", flat=True).first())
    cap = ((agent.interface or {}).get("capabilities") or {}).get(turn or "")
    if not cap or row.app.name not in (cap.get("sites") or []):
        return []
    return list(cap.get("ceiling") or [])


def resolve_for_viewer(session, viewer: Viewer, row: ViewRow) -> host_gateway.SiteContext:
    """Gate 6. The site from the session, the grant from the VIEWER.

    Expiry, resource and DPoP-key checks are `context_for_grant`'s, the same ones
    a visitor's turn passes. Runner requirements do not apply: no runner touches
    a View call; canopy-web makes it directly.
    """
    from .models import HostGrant

    app = row.app
    if not app.issues_host_grants():
        raise ViewRefusal("site_not_configured", f"{app.name} does not let canopy act for you there")
    grants = HostGrant.objects.filter(app=app)
    if viewer.contact is not None:
        grant = grants.filter(contact=viewer.contact).order_by("-updated_at").first()
    else:
        grant = grants.filter(user=viewer.user).order_by("-updated_at").first()
    if grant is None:
        raise ViewRefusal(GATES[6], host_gateway.BACK_ON_THE_PAGE)
    try:
        return host_gateway.context_for_grant(
            app, grant, turn_id="", agent_slug=(session.agent.slug if session.agent_id else "canopy"),
            ceiling=_ceiling(session, row))
    except host_gateway.GatewayRefusal as exc:
        raise ViewRefusal(GATES[6] if exc.code in ("expired", "stale_grant") else exc.code,
                          exc.message) from None


# --- audit ------------------------------------------------------------------------------


def _audit(session, viewer: Viewer, row_ref: dict | None, *, what: str, tool: str = "",
           ok: bool, error: str = "", extra: str = "") -> None:
    from apps.mcp.audit import _write_audit_sync

    ref = row_ref or {}
    summary = (f"session={session.pk} tool_call={ref.get('tool_call_id', '')} "
               f"site={ref.get('site', '')} tool={tool} viewer={viewer.label()} {extra}").strip()
    try:
        _write_audit_sync(user_id=getattr(viewer.user, "pk", None), tool=f"app_view_{what}",
                          args_summary=summary, ok=ok, error=error,
                          workspace=str(session.workspace_id))
    except Exception:  # noqa: BLE001 - audit must never break the call
        log.exception("mcp_apps audit write failed")


def _refuse(session, viewer, ref, what, tool, exc: ViewRefusal):
    _audit(session, viewer, ref, what=what, tool=tool, ok=False, error=exc.code)
    raise exc


# --- the four View requests ---------------------------------------------------------


def resource(session, viewer: Viewer, tool_call_id: str) -> dict:
    """Everything the host page needs to mount one View, read as the viewer.

    The HTML is fetched with the viewer's grant; without one (owner decision 4)
    the cached copy renders READ-ONLY, with a sign-in link and no server tools.
    """
    row = find(session, tool_call_id)
    can_write = viewer.may_act(session)
    ctx, reason = None, ""
    try:
        ctx = resolve_for_viewer(session, viewer, row)
    except ViewRefusal as exc:
        reason = exc.message
    html, meta = None, {}
    if ctx is not None:
        try:
            html, meta = _fetch_view(ctx, row)
        except ViewRefusal as exc:
            reason = exc.message
    entry = mcp_apps.cached_resource(row.app, row.ref["resource_uri"])
    if html is None and entry is not None:
        html = entry["html"]
    if html is None:
        _audit(session, viewer, row.ref, what="resource", ok=False, error="no_resource")
        raise ViewRefusal("no_resource", reason or "this view is not available right now", 404)
    row.app.refresh_from_db(fields=["mcp_apps_index"])
    entry = mcp_apps.cached_resource(row.app, row.ref["resource_uri"]) or {}
    csp = entry.get("csp") or {}
    _audit(session, viewer, row.ref, what="resource", ok=True,
           extra=f"sha256={entry.get('sha256', '')[:16]} live={ctx is not None and not reason}")
    return {
        "tool_call_id": tool_call_id, "site": row.app.name, "tool": row.ref["tool"],
        "resource_uri": row.ref["resource_uri"], "html": html, "csp": csp,
        "sandbox_src": mcp_apps.sandbox_src(csp),
        "prefers_border": entry.get("prefers_border"),
        "can_act": bool(can_write and ctx is not None and not reason),
        "read_only_reason": ("" if can_write else "You can read this conversation but not act in it.")
        or reason,
        "sign_in_url": (row.app.frame_origins() or [""])[0] if (reason and can_write) else "",
        "tool_input": tool_input(row), "tool_result": tool_result(row),
        "receipts": receipts_for(session, tool_call_id),
    }


def _fetch_view(ctx, row: ViewRow) -> tuple[str, dict]:
    try:
        contents = async_to_sync(host_gateway.view_read)(ctx, row.ref["resource_uri"])
    except host_gateway.GatewayRefusal as exc:
        raise ViewRefusal(exc.code, exc.message, 502) from None
    for c in contents:
        if not str(c.get("mimeType") or "").startswith("text/html"):
            continue
        html = c.get("text")
        if html is None and c.get("blob"):
            try:
                html = base64.b64decode(c["blob"]).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                html = None
        if isinstance(html, str) and len(html.encode("utf-8")) <= mcp_apps.MAX_RESOURCE_BYTES:
            meta = c.get("_meta") or {}
            mcp_apps.record_resource(row.app.pk, row.ref["resource_uri"], html=html, meta=meta)
            return html, meta
    raise ViewRefusal("bad_resource", "the site's view was not a usable HTML document", 502)


def call(session, viewer: Viewer, tool_call_id: str, name: str, arguments: dict) -> dict:
    """One `tools/call` from a View, through gates 1-7."""
    ref = None
    try:
        if not viewer.may_act(session):
            raise ViewRefusal(GATES[1], "you can read this conversation but not act in it")
        row = find(session, tool_call_id)
        ref = row.ref
        tool = host_gateway.host_tool_name(name)
        if not tool:
            raise ViewRefusal(GATES[3], "that is not a tool", 422)
        ctx = resolve_for_viewer(session, viewer, row)
        if not ctx.allows(tool):
            raise ViewRefusal(GATES[5], f"{tool} is not allowed on {row.app.name} here")
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "call", name, exc)

    def check(listed):
        if listed is None:
            raise host_gateway.GatewayRefusal(GATES[3], f"{tool} is not a tool of {row.app.name}")
        if not mcp_apps.app_visible(getattr(listed, "meta", None) or {}):
            raise host_gateway.GatewayRefusal(GATES[4], f"{tool} cannot be called from a view")
        check.meta = getattr(listed, "meta", None) or {}

    try:
        result = async_to_sync(host_gateway.view_call)(ctx, tool, arguments or {}, check=check)
    except host_gateway.GatewayRefusal as exc:
        status = 502 if exc.code == "host_unreachable" else 403
        _refuse(session, viewer, ref, "call", tool, ViewRefusal(exc.code, exc.message, status))
    is_error = bool(result.get("isError"))
    _audit(session, viewer, ref, what="call", tool=tool, ok=True, extra=f"is_error={is_error}")
    if mcp_apps.model_visible(getattr(check, "meta", {})):
        _receipt(session, viewer, ref, tool, result)
    return result


def read(session, viewer: Viewer, tool_call_id: str, uri: str) -> dict:
    """A View's own `resources/read`: same server only (the session's site), as the viewer."""
    ref = None
    try:
        row = find(session, tool_call_id)
        ref = row.ref
        ctx = resolve_for_viewer(session, viewer, row)
        contents = async_to_sync(host_gateway.view_read)(ctx, uri)
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "read", uri[:100], exc)
    except host_gateway.GatewayRefusal as exc:
        _refuse(session, viewer, ref, "read", uri[:100], ViewRefusal(exc.code, exc.message, 502))
    _audit(session, viewer, ref, what="read", tool=uri[:100], ok=True)
    return {"contents": contents}


# --- what the agent learns ------------------------------------------------------------


def _update_meta(session, fn) -> None:
    from apps.canopy_sessions.models import Session

    with transaction.atomic():
        locked = Session.objects.select_for_update().get(pk=session.pk)
        meta = dict(locked.metadata or {})
        block = dict(meta.get(META_KEY) or {})
        fn(block)
        meta[META_KEY] = block
        locked.metadata = meta
        locked.save(update_fields=["metadata", "updated_at"])
    session.metadata = locked.metadata


def _excerpt(result: dict) -> str:
    text = " ".join(str(b.get("text") or "") for b in result.get("content") or []
                    if isinstance(b, dict))
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], default=str)
    return text[:RESULT_EXCERPT]


def _receipt(session, viewer: Viewer, ref: dict, tool: str, result: dict) -> None:
    """canopy's own record of a View's commit — the host as witness. A View
    cannot forge or suppress it, whatever it then tells the model."""
    row = {"tool_call_id": ref["tool_call_id"], "site": ref["site"], "tool": tool,
           "by": viewer.ref(), "at": timezone.now().isoformat(),
           "is_error": bool(result.get("isError")), "result": _excerpt(result)}

    def add(block):
        block["receipts"] = ([*(block.get("receipts") or []), row])[-MAX_RECEIPTS:]

    _update_meta(session, add)


def receipts_for(session, tool_call_id: str) -> list[dict]:
    block = (session.metadata or {}).get(META_KEY) or {}
    return [r for r in block.get("receipts") or [] if r.get("tool_call_id") == tool_call_id]


def update_context(session, viewer: Viewer, tool_call_id: str, params: dict) -> None:
    """`ui/update-model-context`: overwrite this View's packet. A cache for the
    next turn, never an authority (`page_state`'s rule), and bounded the same."""
    ref = None
    try:
        if not viewer.may_act(session):
            raise ViewRefusal(GATES[1], "you can read this conversation but not act in it")
        ref = find(session, tool_call_id).ref
        packet = {k: params[k] for k in ("content", "structuredContent") if params.get(k) is not None}
        if len(json.dumps(packet, default=str).encode()) > MAX_CONTEXT_BYTES:
            raise ViewRefusal("too_large", f"model context is limited to {MAX_CONTEXT_BYTES} bytes", 422)
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "context", "", exc)
    entry = {**packet, "site": ref["site"], "tool": ref["tool"], "by": viewer.ref(),
             "at": timezone.now().isoformat()}

    def put(block):
        ctx = dict(block.get("context") or {})
        ctx[tool_call_id] = entry
        if len(ctx) > MAX_CONTEXTS:
            for key in sorted(ctx, key=lambda k: ctx[k].get("at") or "")[: len(ctx) - MAX_CONTEXTS]:
                ctx.pop(key)
        block["context"] = ctx

    _update_meta(session, put)
    _audit(session, viewer, ref, what="context", ok=True)


def check_message(session, viewer: Viewer, tool_call_id: str, text: str) -> dict:
    """Gate a View's `ui/message` (the route sends it, as the viewer)."""
    ref = None
    try:
        if not viewer.may_act(session):
            raise ViewRefusal(GATES[1], "you can read this conversation but not act in it")
        ref = find(session, tool_call_id).ref
        if not (text or "").strip():
            raise ViewRefusal("empty", "message text is required", 422)
        if len(text) > MAX_MESSAGE_CHARS:
            raise ViewRefusal("too_large", f"a view's message is limited to {MAX_MESSAGE_CHARS} characters", 422)
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "message", "", exc)
    _audit(session, viewer, ref, what="message", ok=True)
    return ref


def envelope_block(session) -> dict | None:
    """What the next turn's caller envelope carries (`caller_context.build`):
    each View's latest model context and canopy's recent receipts. None when
    no View has said or done anything in this conversation."""
    if session is None:
        return None
    block = (session.metadata or {}).get(META_KEY) or {}
    context = block.get("context") or {}
    receipts = (block.get("receipts") or [])[-10:]
    if not context and not receipts:
        return None
    return {
        "note": ("Recorded by canopy, not reported by the view: `receipts` are calls a person "
                 "made by clicking in a site's view, as themselves; `context` is what each "
                 "view last said about its state (latest wins)."),
        "context": [{"tool_call_id": k, **v} for k, v in context.items()],
        "receipts": receipts,
    }


# --- keeping the index warm ---------------------------------------------------------


#: Replaced in tests with a function that runs the target inline.
def _spawn(target, *args) -> None:
    from django.conf import settings

    if not getattr(settings, "MCP_APPS_BACKGROUND_REFRESH", True):
        return
    threading.Thread(target=target, args=args, daemon=True).start()


def refresh_index_soon(app, grant) -> None:
    """A visitor just arrived with a grant: list the site's tools as them, in the
    background, so path-D results (the agent's own login, which never lists
    through canopy) find their Views. Skipped while the index is fresh."""
    age = mcp_apps.index_age(app)
    if age is not None and age < host_gateway.INDEX_FRESH:
        return
    _spawn(_refresh, app.pk, grant.pk)


def _refresh(app_id: int, grant_id: int) -> None:
    from django.db import close_old_connections

    from .models import AppCredential, HostGrant

    try:
        app = AppCredential.objects.filter(pk=app_id, revoked_at__isnull=True).first()
        grant = HostGrant.objects.filter(pk=grant_id).first()
        if app is None or grant is None or not app.issues_host_grants():
            return
        ctx = host_gateway.context_for_grant(app, grant, turn_id="", agent_slug="canopy",
                                             ceiling=[])
        async_to_sync(host_gateway.view_list)(ctx)
    except Exception as exc:  # noqa: BLE001 - best effort, never the token
        log.info("mcp_apps index refresh for app %s skipped: %s", app_id, type(exc).__name__)
    finally:
        close_old_connections()
