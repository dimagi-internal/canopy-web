"""MCP Apps: which transcript rows get a View, and what a View may do — as the VIEWER.

Spec 2026-10-08 §3-6 (owner decisions in its last section). The browser routes
that serve a rendered View (`canopy_sessions/app_views_api.py` for canopy users,
`contacts/contact_api.py` for contacts) are thin; this module is the whole rule.

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
from datetime import timedelta

from asgiref.sync import async_to_sync
from canopy_sdk import resources as resource_links
from django.db import transaction
from django.utils import timezone

from apps.harness import sensitivity
from apps.host_grants import host_gateway
from apps.realtime import groups

from . import mcp_apps

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
    #: Whether the request came through a chat surface
    #: (`sensitivity.request_is_chat_surface`). False unless the route says so, so a
    #: sensitive conversation's call arguments reach no token-held door.
    chat_surface: bool = False
    #: Whether the principal behind the request is a person acting as themselves
    #: (`harness.acting_person`) — never an agent's turn, caller token or login. Only
    #: then, and only in a conversation they are in, does canopy tell the host this is
    #: the person's own View (`Canopy-View`, `view_facts`). False unless the route says so.
    person: bool = False
    #: The host grant of the page the request came from (its token's arrival), or
    #: None. A View call from a page holding one is that page acting in the
    #: conversation, so it is stamped there (`note_host_grant`) once the View's own
    #: gates have passed, and the call reads through it.
    page_grant_id: int | None = None

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
    from apps.tokens.models import AppCredential

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


def _use_of(content) -> dict:
    """A tool call as the server knows it: in a sensitive conversation its metadata
    columns alone (`sensitivity.call_of` — the call's id, its name and the host tool a
    gateway call reached), which is all that deciding which View it has needs. Its
    arguments are sealed to the conversation's people: `resource` hands their browser
    the sealed call (`tool_input_sealed`), which it decrypts for the View."""
    return sensitivity.call_of(content)


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
        content = _use_of(getattr(m, "content", None))
        if m.role == Message.TOOL_USE and content.get("id"):
            uses[content["id"]] = content
    wanted = [str((getattr(m, "content", None) or {}).get("tool_use_id") or "") for m in results]
    missing = [w for w in wanted if w and w not in uses]
    if missing:
        for content in (Message.objects.filter(session=session, role=Message.TOOL_USE,
                                               content__id__in=missing)
                        .values_list("content", flat=True)):
            use = _use_of(content)
            uses[use.get("id")] = use
    for m, call_id in zip(results, wanted):
        # A call the host refused gets no View: there is nothing to show or act on, and
        # an empty card beside the agent's corrected retry is noise (2026-10-09: the agent
        # named the action by its type, Labs refused, and a second, failed card appeared).
        if (getattr(m, "content", None) or {}).get("is_error"):
            continue
        use = uses.get(call_id)
        ref = app_ref(app, use) if use else None
        if ref:
            m.app = ref


def app_for_result_block(session, block: dict) -> dict | None:
    """The live-frame form of `annotate`: one tool_result block arriving now."""
    from apps.canopy_sessions.models import Message

    call_id = str((block or {}).get("tool_use_id") or "")
    if not call_id or (block or {}).get("is_error"):
        return None
    app = site_app(session)
    if app is None or not (app.mcp_apps_index or {}).get("tools"):
        return None
    found = (Message.objects.filter(session=session, role=Message.TOOL_USE, content__id=call_id)
             .values_list("content", flat=True).first())
    use = _use_of(found) if found else None
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
    found = (Message.objects.filter(session=session, role=Message.TOOL_USE,
                                    content__id=tool_call_id)
             .values_list("content", flat=True).first()) if app is not None else None
    use = _use_of(found) if found else None
    ref = app_ref(app, use) if use else None
    found = (Message.objects.filter(session=session, role=Message.TOOL_RESULT,
                                    content__tool_use_id=tool_call_id)
             .values_list("content", "plaintext").first()) if ref else None
    result = None
    if found is not None:
        content, plaintext = found
        result = dict(content or {})
        # The runner's transcript rows keep a result's TEXT in `plaintext`, with only
        # {is_error, tool_use_id} in `content` -- so a path-D View got an empty result
        # and fell back on a depth-elided input copy (Labs' coaching card, 2026-10-09).
        if not result.get("content") and plaintext:
            result["content"] = plaintext
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
    kept = _kept_links(row.result)
    if row.ref["path"] == "gateway":
        for b in blocks:
            try:
                data = json.loads(b.get("text") or "")
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and ("content" in data or "links" in data):
                links = [x for x in data.get("links") or [] if isinstance(x, dict)]
                content = [x for x in data.get("content") or [] if isinstance(x, dict)]
                structured = data.get("structured")
                if structured is not None and not any(x.get("type") == "text" for x in content):
                    # `site_call` drops MCP's text copy of `structured` (`agent_answer`);
                    # a View written to the spec may read either, so it gets both back.
                    content.append({"type": "text", "text": json.dumps(structured)})
                out = {"content": _with_links(links + content, kept),
                       "isError": bool(data.get("is_error"))}
                if structured is not None:
                    out["structuredContent"] = structured
                return out
    # Path D, or a gateway answer whose text was cut (a row keeps TOOL_TEXT_MAX of it):
    # the links the runner kept whole carry the View to the host's result regardless.
    return {"content": _with_links(blocks, kept), "isError": is_error}


def _kept_links(result: dict) -> list[dict]:
    """The `resource_links` a row kept whole (`canopy_transcript.tool_result_links`, or
    `sensitivity.strip_result` for a sensitive conversation), as canopy keeps a link."""
    links = result.get(sensitivity.RESOURCE_LINKS)
    return resource_links.validate_links(links, None) if isinstance(links, list) else []


def _with_links(blocks: list[dict], kept: list[dict]) -> list[dict]:
    """`blocks` with every kept link not already among them, links first."""
    have = {b.get("uri") for b in blocks if b.get("type") == resource_links.RESOURCE_LINK}
    return [link for link in kept if link["uri"] not in have] + blocks


#: `_meta` on a sensitive conversation's tool result: canopy did not keep it.
WITHHELD_META = "canopy/withheld"


def withheld_result(row: ViewRow) -> dict | None:
    """A sensitive conversation's tool result as its View receives it: the host's
    resource links and nothing else, or None when the call left none."""
    links = _kept_links(row.result or {})
    if not links:
        return None
    return {"content": links,
            "structuredContent": None, "_meta": {WITHHELD_META: True},
            "isError": bool((row.result or {}).get("is_error"))}


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


def view_facts(session, viewer: Viewer, grant) -> dict | None:
    """What `Canopy-View` states for this viewer's reads, or None: canopy says "the
    person's own View, no agent acting" only when the principal behind the request is a
    person acting as themselves (`Viewer.person`, from `harness.acting_person`), they
    are IN this conversation (`sensitivity.is_conversation_person`) and, for a sensitive
    one, reading it on its chat surface (`may_read`'s rule). The person is named
    as the host knows them (the grant's `subject`); the resource is bound per read."""
    if not viewer.person or not grant.subject:
        return None
    if not sensitivity.is_conversation_person(session, user=viewer.user, contact=viewer.contact):
        return None
    # The same people `may_read` lets read a sensitive conversation's results, no wider.
    if not sensitivity.reads_content(session, chat_surface=viewer.chat_surface, user=viewer.user,
                                     contact=viewer.contact):
        return None
    return {"subject": grant.subject, "session": str(session.pk), "viewer": viewer.label(),
            "agent": session.agent.slug if session.agent_id else ""}


def resolve_for_viewer(session, viewer: Viewer, row: ViewRow) -> host_gateway.SiteContext:
    """Gate 6. The site from the session, the grant from the VIEWER.

    Expiry, resource and DPoP-key checks are `context_for_grant`'s, the same ones
    a visitor's turn passes. Runner requirements do not apply: no runner touches
    a View call; canopy-web makes it directly.
    """
    app = row.app
    if not app.issues_host_grants():
        raise ViewRefusal("site_not_configured", f"{app.name} does not let canopy act for you there")
    if viewer.page_grant_id:
        from apps.canopy_sessions.services import note_host_grant  # noqa: PLC0415 — as Message here

        note_host_grant(session, viewer.page_grant_id, user=viewer.user if viewer.contact is None else None,
                        contact=viewer.contact)
    # The viewer's grant for THIS conversation's agent, from the page that last
    # acted in it as them (`host_grants.lookup`) — the View's own call stamped it
    # first, when it came from a page holding one.
    from apps.host_grants.host_grants import lookup

    found = lookup(app, agent=session.agent if session.agent_id else None,
                   contact=viewer.contact, user=viewer.user if viewer.contact is None else None,
                   session=session)
    grant = found.grant
    if grant is None:
        raise ViewRefusal(GATES[6], host_gateway.no_grant_message(
            app.name, found.reason, (session.agent.name or session.agent.slug) if session.agent_id else "the agent"))
    try:
        return host_gateway.context_for_grant(
            app, grant, turn_id="", agent_slug=(session.agent.slug if session.agent_id else "canopy"),
            ceiling=_ceiling(session, row), view=view_facts(session, viewer, grant))
    except host_gateway.GatewayRefusal as exc:
        raise ViewRefusal(GATES[6] if exc.code in ("expired", "revoked", "stale_grant") else exc.code,
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
        # A sensitive conversation stores no result (spec 2026-10-22-sensitive-data-agents
        # §4): the View fetches its data as the viewer, from the input. That input is
        # sealed to the conversation's people (spec 2026-10-23 §2), so canopy-web sends
        # it sealed — to a reader `reads_content` admits — and their browser decrypts
        # it (`tool_input_sealed`; `tool_input_path` says where the arguments sit) and
        # hands it to the View. An ordinary conversation's input goes as it is.
        "tool_input": ({} if sensitivity.is_sensitive(session) else tool_input(row)),
        "tool_input_sealed": sensitivity.sealed_call_for(
            session, tool_call_id, chat_surface=viewer.chat_surface, user=viewer.user,
            contact=viewer.contact),
        "tool_input_path": row.ref["path"],
        # A sensitive conversation keeps no result, only the host's resource links
        # (`sensitivity.strip_result`): the View gets those — addresses it reads back
        # from the host as the viewer — and `canopy/withheld` says the rest was not
        # kept (spec 2026-10-23-host-held-results §3). An ordinary conversation's
        # result goes as the transcript holds it, links included.
        "tool_result": withheld_result(row) if sensitivity.is_sensitive(session) else tool_result(row),
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
        annotations = getattr(listed, "annotations", None)
        # MCP SDK v2 names it read_only_hint; older clients readOnlyHint.
        check.read_only = bool(
            getattr(annotations, "read_only_hint", None) if hasattr(annotations, "read_only_hint")
            else getattr(annotations, "readOnlyHint", False)
        )

    try:
        result = async_to_sync(host_gateway.view_call)(ctx, tool, arguments or {}, check=check,
                                                       view_uri=row.ref.get("resource_uri") or "")
    except host_gateway.GatewayRefusal as exc:
        status = 502 if exc.code == "host_unreachable" else 403
        _refuse(session, viewer, ref, "call", tool, ViewRefusal(exc.code, exc.message, status))
    is_error = bool(result.get("isError"))
    _audit(session, viewer, ref, what="call", tool=tool, ok=True, extra=f"is_error={is_error}")
    # A receipt is canopy's witness of a person CHANGING something from a View. A call the
    # host declares read-only (MCP readOnlyHint) -- a status poll -- is audited but not
    # recorded in the chat (2026-10-09: "Done: workflow_action_status" beside each send).
    if mcp_apps.model_visible(getattr(check, "meta", {})) and not getattr(check, "read_only", False):
        _receipt(session, viewer, ref, tool, result)
    return result


def may_read(session, viewer: Viewer) -> None:
    """Who may read from the host through a View (`read`) or hear that it changed
    (`subscribe`). Not `may_act`: a read changes nothing and runs on the viewer's OWN
    grant, under the host's own ACL, so anyone the session ACL lets see an ordinary
    conversation may (a VIEWER participant reads their own share of the host). A
    sensitive conversation's results are read only by its own people on its own chat
    surfaces (`sensitivity.reads_content`), the rule its words follow. Raises."""
    if not sensitivity.reads_content(session, chat_surface=viewer.chat_surface, user=viewer.user,
                                     contact=viewer.contact):
        raise ViewRefusal("viewer_cannot_read",
                          "only the people in this conversation read its results, from its chat")


def read(session, viewer: Viewer, tool_call_id: str, uri: str) -> dict:
    """A View's own `resources/read`: same server only (the session's site), as the viewer,
    by whoever `may_read`."""
    ref = None
    try:
        may_read(session, viewer)
        row = find(session, tool_call_id)
        ref = row.ref
        ctx = resolve_for_viewer(session, viewer, row)
        contents = async_to_sync(host_gateway.view_read)(ctx, uri)
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "read", uri[:100], exc)
    except host_gateway.GatewayRefusal as exc:
        _refuse(session, viewer, ref, "read", uri[:100], ViewRefusal(
            exc.code, exc.message, 502 if exc.code == "host_unreachable" else 403))
    _audit(session, viewer, ref, what="read", tool=uri[:100], ok=True)
    return {"contents": contents}


# --- resources/subscribe: a View hears when a host resource changes -------------------

#: How long a subscription lasts unless the View subscribes again.
SUBSCRIPTION_TTL = timedelta(hours=24)
#: The most live subscriptions one conversation holds.
MAX_SUBSCRIPTIONS = 100
MAX_URI_CHARS = 2048
#: The socket frame a View's host page receives (`consumers.app_resource_updated`).
RESOURCE_UPDATED = "app.resource_updated"


def _subscription_uri(uri) -> str:
    uri = str(uri or "").strip()
    if not uri or len(uri) > MAX_URI_CHARS or ":" not in uri or any(c.isspace() for c in uri):
        raise ViewRefusal("bad_uri", "that is not a resource URI", 422)
    return uri


def subscribe(session, viewer: Viewer, tool_call_id: str, uri: str) -> dict:
    """A View's `resources/subscribe`: canopy holds the subscription (the gateway's
    MCP sessions are per request, so no host session outlives the call) and relays
    the host's signed "it changed" to this View (`resources_updated`).

    Gated like `read`: `may_read`, a real View row in this session and the viewer's own
    live grant for its site. What reaches the View later is the URI alone; the re-read
    goes through `read`, as the viewer, under the host's ACL."""
    from apps.canopy_sessions.models import AppViewSubscription  # noqa: PLC0415 — as Message here: models after the registry

    ref = None
    try:
        may_read(session, viewer)
        row = find(session, tool_call_id)
        ref = row.ref
        uri = _subscription_uri(uri)
        resolve_for_viewer(session, viewer, row)
        now = timezone.now()
        AppViewSubscription.objects.filter(session=session, expires_at__lte=now).delete()
        live = AppViewSubscription.objects.filter(session=session)
        if (not live.filter(tool_call_id=tool_call_id, uri=uri).exists()
                and live.count() >= MAX_SUBSCRIPTIONS):
            raise ViewRefusal("too_many", f"a conversation holds at most {MAX_SUBSCRIPTIONS} subscriptions", 429)
    except ViewRefusal as exc:
        _refuse(session, viewer, ref, "subscribe", str(uri)[:100], exc)
    AppViewSubscription.objects.update_or_create(
        session=session, tool_call_id=tool_call_id, uri=uri,
        defaults={"app": row.app, "expires_at": now + SUBSCRIPTION_TTL})
    _audit(session, viewer, ref, what="subscribe", tool=uri[:100], ok=True)
    return {}


def unsubscribe(session, viewer: Viewer, tool_call_id: str, uri: str) -> dict:
    """A View's `resources/unsubscribe`. Idempotent; needs only the session's ACL
    (the route's), so a View whose grant lapsed can still stop listening."""
    from apps.canopy_sessions.models import AppViewSubscription  # noqa: PLC0415 — as above

    AppViewSubscription.objects.filter(session=session, tool_call_id=tool_call_id,
                                       uri=str(uri or "").strip()).delete()
    return {}


def sweep_subscriptions() -> int:
    """Delete every lapsed subscription, in every conversation (the off-request clock,
    `apps.common.ticks`). `subscribe` also clears its own conversation's, but a
    conversation nobody subscribes in again would keep its rows for ever."""
    from apps.canopy_sessions.models import AppViewSubscription  # noqa: PLC0415 — as above

    deleted, _ = AppViewSubscription.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted


def resources_updated(app, uris) -> int:
    """The host said `uris` changed (a request signed by its own key, verified by the
    route): tell every View subscribed to one of them on THIS site. The frame carries
    the call id and the URI, never data. Returns how many Views were told."""
    from apps.canopy_sessions.models import AppViewSubscription  # noqa: PLC0415 — as above

    subs = list(AppViewSubscription.objects.filter(app=app, uri__in=list(uris),
                                                   expires_at__gt=timezone.now())
                .values_list("session_id", "tool_call_id", "uri"))
    for session_id, tool_call_id, uri in subs:
        groups.publish(groups.session_group(session_id),
                       {"type": "app.resource_updated", "tool_call_id": tool_call_id, "uri": uri})
    return len(subs)


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
           "is_error": bool(result.get("isError")),
           "result": "" if sensitivity.is_sensitive(session) else _excerpt(result)}

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
    if sensitivity.is_sensitive(session):
        # A View's state is the host's data; a sensitive conversation does not keep
        # it (§4). Accepted and dropped, so the View carries on.
        _audit(session, viewer, ref, what="context", ok=True, extra="withheld=1")
        return
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

    from apps.tokens.models import AppCredential, HostGrant

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
