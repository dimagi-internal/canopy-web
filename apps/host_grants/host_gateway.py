"""canopy-web as the gateway between a visitor's turn and the host's MCP server.

Host grant contract v1, §3. In a turn for a visitor who arrived with a host
grant (`host_grants.py`), the agent reaches the host's tools THROUGH canopy's own
MCP (`site_tools` / `site_call`, `apps/mcp/tools/site.py`), and this module is
what those tools do:

1. **Resolve** the turn to its site and its visitor — from the turn itself
   (`initiator_*`, the conversation's server-owned `embed_app`), never from an
   argument the agent supplied.
2. **Refuse** unless every one of these holds, each with a sentence the agent
   can say to the visitor: the conversation was held on a live connected site
   that issues host grants; the turn's capability names that site in `sites:` (or,
   for a full-profile turn — the owner's, an admin's, an editor's — some capability
   of the agent does: a chat on a page reaches that site only as the person who
   sent, owners included);
   this visitor holds a grant for it; the grant is unexpired, for the site's
   current resource, and bound to canopy's current DPoP key; and the tool is
   inside the effective set (below).
3. **Call** the host over Streamable HTTP with `Authorization: DPoP <token>`, a
   fresh proof per request (`htm`, `htu`, `ath`, `iat`, `jti`), and
   `Canopy-Actor: <agent slug>` for the host's audit.

**There is no fallback.** An expired or missing grant is a refusal that asks for
the visitor to be back on the page. It is never a call made with the agent's own
credential, which would hand the visitor whatever the agent can reach — the same
"409 and no shared fallback" rule as the GitHub delegation.

**The token never leaves this process.** It is decrypted here, sent to the one
URL it is audience-bound to, and dropped. Nothing about it goes into a return
value, an exception message, a log line or the audit row.

**The effective set is the host's.** With a host grant every call runs AS the
visitor, and the host both lists (`tools/list`) and enforces (per call) only the
tools the grant's scopes map to, under that person's own ACL. So what a turn may
call is what the host lists for this token. canopy adds a narrowing only on the
agent owner's word:

* the owner's per-site **ceiling** (`ceiling:` on the capability) — OPTIONAL. Absent
  or empty, canopy adds nothing and the host's grant decides; present, it narrows
  (an owner keeping an agent off part of a site). It can never widen: the host
  still refuses anything outside the grant.

The page's `backing_tool(s)` are a HINT — carried to the agent in its caller
context so it knows where to read the rows on screen — and no longer a filter.
They were one until 2026-09-29, but page state is written by the page's own
JavaScript, so that filter never bounded a hostile page (the grant's scopes do),
and it made every host re-list in its page state the tools its own grant had
already decided — canopy holding the host's tool names twice over.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import timedelta

from asgiref.sync import sync_to_async
from canopy_sdk import consumer, contract
from canopy_sdk import resources as resource_links
from django.db import transaction
from django.utils import timezone

from apps.agents import sensitive_data

from . import execution

#: A grant this close to expiry is treated as expired: the call would race it.
EXPIRY_MARGIN = timedelta(seconds=10)
#: Header the host logs as the acting agent. Informational: not trusted for authz.
#: The SDK's constant — the host's side reads the same one.
ACTOR_HEADER = contract.ACTOR_HEADER
BACK_ON_THE_PAGE = ("I need you back on the page to do that — ask again from the site "
                    "and I will try once more.")
#: The person's act-as-you permission for this page (its host grant) ran out
#: (`host_grants.EXPIRED`): no other page's grant stands in. Sending again from the
#: page grants it afresh (the panel asks its host for a grant before that send).
def page_grant_expired(agent_name: str) -> str:
    return (f"Your permission for {agent_name} to act as you on this page has ended — send "
            "again to grant it for another 30 minutes.")


#: The person stopped it (`host_grants.REVOKED`).
def page_grant_stopped(agent_name: str) -> str:
    return (f"You stopped {agent_name}'s permission to act as you on this page — send again "
            "to grant it for another 30 minutes.")


#: The page asked, and the person has not confirmed it with their passkey
#: (`host_grants.UNCONFIRMED`).
def page_grant_unconfirmed(agent_name: str) -> str:
    return (f"{agent_name} can act as you on this page only once you confirm it with your "
            "passkey — send again and confirm when the panel asks.")


#: An agent's turn needed the page's permission, still waiting for the passkey, and
#: canopy has just asked the person in the panel (`host_grants.ask`). Said to the
#: agent, which can wait and retry: the person may be confirming right now.
def page_grant_asked(agent_name: str) -> str:
    return (f"I have asked the person to confirm with their passkey, in the panel, that "
            f"{agent_name} may act as them on this page. Wait about 20 seconds and try this "
            "call again; if it is still not confirmed after a few tries, tell them what you "
            "need it for and carry on without it.")


def no_grant_message(site: str, reason: str, agent_name: str = "the agent") -> str:
    """What to tell the person when `host_grants.lookup` found no grant, by its reason."""
    from .host_grants import EXPIRED, REVOKED, UNCONFIRMED  # noqa: PLC0415 — as `resolve`: host_grants reads models, loaded with a call

    if reason == EXPIRED:
        return page_grant_expired(agent_name)
    if reason == REVOKED:
        return page_grant_stopped(agent_name)
    if reason == UNCONFIRMED:
        return page_grant_unconfirmed(agent_name)
    return f"{site} has not given me access on your behalf — " + BACK_ON_THE_PAGE


class GatewayRefusal(Exception):
    """Why this call will not go through. The message is safe to show the agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SiteContext:
    """Everything a gateway call needs, resolved server-side for ONE turn."""

    turn_id: str
    agent_slug: str
    site: str
    resource: str
    scope: str
    expires_at: object
    ceiling: list[str]
    _token: str = field(repr=False, default="")
    #: The Connected site's row, so an audit line names the site unambiguously
    #: (a site NAME is unique only per tenant).
    app_id: int | None = None
    #: What `Canopy-Execution` states for this turn (`execution.facts_for`), signed
    #: afresh on every request. None for a call no agent turn makes (a View, a probe).
    execution: dict | None = field(repr=False, default=None)
    #: The conversation the turn is in (`resolve`), whose returned links `site_read` may
    #: read back. "" for a context no turn made.
    session_id: str = ""
    #: What `Canopy-View` states — the person's own View, no agent acting
    #: (`mcp_apps_views.view_facts`: subject, session, viewer, agent) — signed afresh on
    #: every request of a View's `resources/read`, bound to the URI it reads. None for
    #: anything else; never set beside `execution` (`resolve` never sets it).
    view: dict | None = field(repr=False, default=None)

    def allows(self, tool: str) -> bool:
        return tool_allowed(tool, ceiling=self.ceiling)


# --- names --------------------------------------------------------------------


def host_tool_name(name: str) -> str:
    """A tool's name AT THE HOST, from however it is written here.

    Owners write ceilings in Claude Code's naming (`mcp__*connect_labs__*`), or
    bare (`marketplace_*`). The server segment is dropped: the gateway only
    ever calls one server, the site's own, so the host's tool name is what
    matters.
    """
    value = (name or "").strip()
    if value.startswith("mcp__"):
        parts = value.split("__", 2)
        return parts[2] if len(parts) == 3 else ""
    if "__" in value:
        return value.rsplit("__", 1)[1]
    return value


def tool_allowed(tool: str, *, ceiling: list[str]) -> bool:
    """Inside the owner's ceiling, if the owner set one.

    An empty ceiling narrows nothing: the host's grant decides (it lists and
    enforces its own tools, as the visitor). A tool with no name is never allowed.
    """
    name = host_tool_name(tool)
    if not name:
        return False
    globs = [g for g in (host_tool_name(c) for c in ceiling or []) if g]
    if not globs:
        return True
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


# --- resolution ----------------------------------------------------------------


def resolve(turn_id: str, *, reason: str = "") -> SiteContext:
    """The site, grant and effective tools for this turn — or a refusal.

    `reason` is what the agent says it needs the person's permission for; when the
    page's permission still waits for their passkey it is put in front of them in the
    panel's prompt (`host_grants.ask`, pushed by `_push_ask`)."""
    from apps.harness.models import Turn

    from apps.tokens.models import AppCredential

    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__agent",
                                        "initiator_contact", "initiator_user",
                                        "claimed_by")
            .prefetch_related("claimed_by__declared_flags")
            .filter(pk=turn_id).first())
    if turn is None:
        raise GatewayRefusal("no_turn", "turn not found")
    session = turn.chat_session if turn.chat_session_id else None
    agent = turn.acting_agent
    if agent is None or session is None:
        raise GatewayRefusal("no_site", "this conversation was not held on a connected site")

    from apps.harness import runner_requirements as rr

    # Defence in depth: routing should make this unreachable. This is where the
    # host's data enters a turn, so a routing bug fails as a refusal, not a leak.
    reqs = rr.requirements_of_session(session)
    if reqs and (turn.claimed_by is None or not rr.runner_satisfies(turn.claimed_by, reqs)):
        raise GatewayRefusal(
            "runner_requirements",
            f"this conversation must run on a {rr.describe(reqs)} runner, and this one is not")

    site = str((session.metadata or {}).get("embed_app") or "").strip()
    if not site:
        raise GatewayRefusal("no_site", "this conversation was not held on a connected site")
    # The site's row in the AGENT's tenant, live. Name + tenant, never an id
    # anyone outside canopy supplied.
    app = AppCredential.objects.filter(name=site, workspace_id=agent.workspace_id,
                                       revoked_at__isnull=True).first()
    if app is None or not app.issues_host_grants():
        raise GatewayRefusal("site_not_configured",
                             f"{site} does not let me act for you there")

    from apps.agents import interface  # noqa: PLC0415 — as the model reads above: loads with a call

    if turn.capability:
        # A confined turn: its own capability must name the site.
        cap = ((agent.interface or {}).get("capabilities") or {}).get(turn.capability)
        if not cap or site not in (cap.get("sites") or []):
            raise GatewayRefusal("site_not_in_capability",
                                 f"I am not set up to use {site}'s tools in this conversation")
        ceiling = list(cap.get("ceiling") or [])
    else:
        # A FULL-profile turn (the owner, an admin, an editor) in a conversation held
        # on a site's page: it reaches the site the same way a visitor's does — as the
        # person who sent, under their act-as-you permission for the page, never with
        # the agent's own login there (withheld from the turn, `interface.own_login_of`).
        # Only on a site the agent is set up for (some capability lists it).
        if interface.page_site_of(turn, agent) != site:
            raise GatewayRefusal("site_not_in_capability",
                                 f"I am not set up to use {site}'s tools in this conversation")
        ceiling = interface.site_ceiling(agent, site)

    # WHO ASKED, from the turn — the grant is theirs or there is none. And for
    # WHICH agent: a grant a page bound to another agent is not this one's. And
    # from WHICH page: the one that last acted in this conversation as them, while
    # it lives — never another page's, never their latest.
    from .host_grants import EXPIRED, REVOKED, UNCONFIRMED, ask, lookup  # noqa: PLC0415 — host_grants reads models, loaded with a call

    found = lookup(app, agent=agent, contact=turn.initiator_contact,
                   user=turn.initiator_user if not turn.initiator_contact_id else None,
                   session=session)
    grant = found.grant
    if grant is None and found.waiting is not None:
        # The page holds a permission the person has not confirmed: the panel asks
        # for their passkey only now, when this turn actually needs it.
        ask(found.waiting, reason)
        _push_ask(session, found.waiting)
        raise GatewayRefusal("unconfirmed", page_grant_asked(agent.name or agent.slug))
    if grant is None:
        code = {EXPIRED: "expired", REVOKED: "revoked",
                UNCONFIRMED: "unconfirmed"}.get(found.reason, "no_grant")
        raise GatewayRefusal(code, no_grant_message(site, found.reason, agent.name or agent.slug))

    # The grant is per arrival, not per conversation: a visitor who
    # arrived under `sensitive_data` can reach it from an OLDER conversation that no
    # such arrival stamped. So the grant's own requirements count too, and they are
    # written onto the conversation — refused or not — so its next turn routes
    # to a runner that satisfies them.
    grant_reqs = rr.requirements_of_grant(grant)
    if grant_reqs - reqs:
        from apps.canopy_sessions.services import add_runner_requirements

        add_runner_requirements(session, grant_reqs)
    effective = reqs | grant_reqs
    if effective and (turn.claimed_by is None
                      or not rr.runner_satisfies(turn.claimed_by, effective)):
        raise GatewayRefusal(
            "runner_requirements",
            f"this conversation must run on a {rr.describe(effective)} runner, and this one is not")
    # A sensitive conversation's data reaches only a capability its interface says
    # handles sensitive data (`data: sensitive`; spec 2026-10-22-sensitive-data-agents §6).
    if rr.SENSITIVE_DATA in effective and not sensitive_data.handles_sensitive(agent, turn.capability):
        raise GatewayRefusal(
            "undeclared",
            f"{site} gives its sensitive data only to a capability that handles sensitive data "
            "(`data: sensitive`), and this turn's does not; ask the agent's owner to set it")
    ctx = context_for_grant(app, grant, turn_id=str(turn.pk), agent_slug=agent.slug,
                            ceiling=ceiling,
                            execution=execution.facts_for(turn, agent, session, effective))
    ctx.session_id = str(session.pk)
    return ctx


def _push_ask(session, grant) -> None:
    """Tell the person's own panel in this conversation, at once, that the agent asked
    (the conversation's socket: `SessionConsumer.host_grant_requested` passes it only
    to the grant's own person). It carries the grant's id and nothing else: the panel
    re-reads the grant, reason included, through its own token."""
    from apps.realtime.groups import publish, session_group  # noqa: PLC0415 — as this module's other reads: loads with a call

    publish(session_group(session.id),
            {"type": "host_grant.requested", "grant_id": grant.pk, "user_id": grant.user_id})


def context_for_grant(app, grant, *, turn_id: str, agent_slug: str, ceiling: list[str],
                      execution: dict | None = None, view: dict | None = None) -> SiteContext:
    """A `SiteContext` for ONE stored grant — or a refusal if the grant cannot
    be used: expired, for a resource the site no longer names, or bound to a
    DPoP key canopy no longer holds. `resolve` reaches it for a visitor's turn;
    the live probe (`live_probe.py`) reaches it for the probe's own grant, so
    both are held to the same checks and call through the same client."""
    from apps.common.encryption import decrypt_secret

    from . import client_identity

    if grant.revoked_at is not None:
        raise GatewayRefusal("revoked", page_grant_stopped(agent_slug))
    if grant.confirmed_at is None:
        raise GatewayRefusal("unconfirmed", page_grant_unconfirmed(agent_slug))
    if grant.expires_at <= timezone.now() + EXPIRY_MARGIN:
        raise GatewayRefusal("expired", page_grant_expired(agent_slug))
    if grant.resource.rstrip("/") != app.host_mcp_resource.rstrip("/"):
        raise GatewayRefusal("stale_grant", BACK_ON_THE_PAGE)
    try:
        jkt = client_identity.dpop_jkt()
    except client_identity.ClientIdentityError:
        raise GatewayRefusal("no_client_key", "canopy cannot reach sites on anyone's behalf "
                             "right now") from None
    if grant.dpop_jkt != jkt:
        raise GatewayRefusal("stale_grant", BACK_ON_THE_PAGE)

    return SiteContext(
        turn_id=turn_id, agent_slug=agent_slug, site=app.name,
        resource=app.host_mcp_resource, scope=grant.scope, expires_at=grant.expires_at,
        ceiling=list(ceiling),
        _token=decrypt_secret(grant.access_token_enc), app_id=app.pk, execution=execution,
        view=view if execution is None else None,
    )


# --- the call ------------------------------------------------------------------

#: Tests replace this with an in-process transport (an ASGI app standing in for
#: the host). Production never sets it.
_transport_override = None


#: The most bytes canopy reads of one answer from a host (`HOST_GATEWAY_MAX_RESPONSE_BYTES`).
#: A result a host keeps is paged by its reader; one that arrives bigger than this is a
#: request for too much, refused rather than held in memory and handed to an agent or a View.
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TOO_LARGE = "result too large; use a smaller page_size"


def max_response_bytes() -> int:
    from django.conf import settings  # noqa: PLC0415 — read per call, so a test or an operator can change it

    return int(getattr(settings, "HOST_GATEWAY_MAX_RESPONSE_BYTES", DEFAULT_MAX_RESPONSE_BYTES)
               or DEFAULT_MAX_RESPONSE_BYTES)


#: What canopy asks a host for and accepts back: no content coding. The cap counts the
#: bytes canopy will hold, so a compressed body (40 KB of gzip is 40 MB in memory) is
#: refused rather than counted small and decoded big.
IDENTITY = "identity"
ENCODED = "answered in a compressed encoding canopy does not accept"


class _ByteCap:
    """One MCP session's byte budget for what the host sends back: counted as the body
    STREAMS in (`_CappedTransport`), so an answer over the cap is abandoned mid-stream,
    never buffered whole. The request asks for `Accept-Encoding: identity` and an answer
    with any other `Content-Encoding` is refused, so the bytes counted are the bytes
    decoded. `tripped` survives the MCP client's own error handling, which turns any
    stream failure into a generic one."""

    def __init__(self, limit: int | None = None):
        self.limit = int(limit or max_response_bytes())
        self.tripped = False
        self.encoded = False

    def refusal(self, ctx: SiteContext) -> GatewayRefusal:
        if self.encoded:
            return GatewayRefusal("bad_encoding", f"{ctx.site} {ENCODED}")
        return GatewayRefusal("too_large", f"{ctx.site}'s {TOO_LARGE}")


def _capped_transport(inner, cap: _ByteCap):
    import httpx2  # noqa: PLC0415 — as _client: the HTTP client loads with a call

    class _TooLarge(httpx2.StreamError):
        pass

    class _Stream(httpx2.AsyncByteStream):
        def __init__(self, stream):
            self._stream = stream
            self._seen = 0

        async def __aiter__(self):
            async for chunk in self._stream:
                self._seen += len(chunk)
                if self._seen > cap.limit:
                    cap.tripped = True
                    raise _TooLarge(TOO_LARGE)
                yield chunk

        async def aclose(self):
            await self._stream.aclose()

    class _Transport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            request.headers["Accept-Encoding"] = IDENTITY
            response = await inner.handle_async_request(request)
            coding = response.headers.get("content-encoding", "").strip().lower()
            if coding not in ("", IDENTITY):
                cap.tripped = cap.encoded = True
                await response.aclose()
                raise _TooLarge(ENCODED)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > cap.limit:
                cap.tripped = True
                await response.aclose()
                raise _TooLarge(TOO_LARGE)
            return httpx2.Response(status_code=response.status_code, headers=response.headers,
                                   stream=_Stream(response.stream), extensions=response.extensions,
                                   request=request)

        async def aclose(self):
            await inner.aclose()

    return _Transport()


#: How a request proves possession. Only `dpop` is ever used for a real call;
#: the rest exist so the live probe can show the host REFUSES a bound token
#: without a valid proof — through this same client, not a hand-built request.
PROOF_MODES = ("dpop", "no_proof", "stranger_key", "bearer")


def _auth(ctx: SiteContext, mode: str = "dpop", view_uri: str = "", view_tool: ViewTool | None = None):
    import httpx2

    from . import client_identity

    if mode not in PROOF_MODES:
        raise ValueError(f"unknown proof mode {mode!r}")
    token = ctx._token
    stranger = None
    if mode == "stranger_key":
        from canopy_sdk.keys import generate_private_key

        stranger = generate_private_key("EdDSA")

    class _DPoP(httpx2.Auth):
        """A fresh proof on EVERY request of the MCP session (initialize,
        list, call...), each bound to that request's method and URL and to the
        token (`ath`). Retries once on a `use_dpop_nonce` challenge."""

        def auth_flow(self, request):
            htu = str(request.url.copy_with(query=None, fragment=None))
            if mode == "bearer":
                request.headers["Authorization"] = f"Bearer {token}"
                yield request
                return
            request.headers["Authorization"] = consumer.dpop_authorization(token)
            if mode == "stranger_key":
                request.headers[contract.DPOP_HEADER] = consumer.dpop_proof(
                    stranger, request.method, htu, access_token=token)
            elif mode == "dpop":
                request.headers[contract.DPOP_HEADER] = client_identity.dpop_proof(
                    request.method, htu, access_token=token)
                _state(request)
            response = yield request
            nonce = response.headers.get(contract.DPOP_NONCE_HEADER)
            if mode == "dpop" and response.status_code == 401 and nonce:
                request.headers[contract.DPOP_HEADER] = client_identity.dpop_proof(
                    request.method, htu, access_token=token, nonce=nonce)
                _state(request)  # a fresh jti: the first was spent, or may have been
                yield request

    def _state(request):
        # canopy vouches for THIS request: a fresh, single-use statement each time. An
        # agent's turn says which agent (`Canopy-Execution`); the person's own View says
        # so (`Canopy-View`), for the one resource it reads, or for the one tool its card
        # calls (`uri` then the View's own `ui://` resource). Never both, and nothing for
        # anything else — a host must treat no statement as no one it may trust.
        # Each is bound to THIS JSON-RPC request too (`rpc`: its method and params), so a
        # statement lifted off one call cannot ride another on the same token.
        try:
            body = request.content if request.method == "POST" else b""
        except Exception:  # noqa: BLE001 - a streamed body: bound to nothing, so it vouches for nothing
            body = b""
        rpc = contract.rpc_digest_of_body(body)
        if ctx.execution is not None:
            request.headers[contract.EXECUTION_HEADER] = client_identity.execution_statement(
                ctx.resource, ctx.execution, access_token=token, rpc=rpc)
        elif ctx.view is not None and view_uri and _reads(body, view_uri):
            request.headers[contract.VIEW_HEADER] = client_identity.view_statement(
                ctx.resource, uri=view_uri, access_token=token, rpc=rpc, **ctx.view)
        elif ctx.view is not None and view_tool is not None and view_tool.uri \
                and _calls(body, view_tool.name):
            request.headers[contract.VIEW_HEADER] = client_identity.view_statement(
                ctx.resource, uri=view_tool.uri, access_token=token, rpc=rpc, **ctx.view)

    return _DPoP()


class ViewTool:
    """The one `tools/call` a person's View is making (`view_call`): the tool, and the
    View it belongs to — its `uri`, set once the listing shows the tool's
    `_meta.ui.resourceUri` is the card's own View. Empty, no statement rides."""

    __slots__ = ("name", "uri")

    def __init__(self, name: str):
        self.name, self.uri = name, ""


def _calls(body: bytes, name: str) -> bool:
    """Whether this request body is the `tools/call` of `name`."""
    rpc = contract.rpc_of_body(body)
    return bool(rpc and rpc[0] == "tools/call" and str(rpc[1].get("name") or "") == name)


def _reads(body: bytes, uri: str) -> bool:
    """Whether this request body is the `resources/read` of `uri` (the only request a
    View's statement rides — never its `initialize` or anything else)."""
    rpc = contract.rpc_of_body(body)
    return bool(rpc and rpc[0] == "resources/read" and str(rpc[1].get("uri") or "") == uri)


def _client(ctx: SiteContext, *, mode: str = "dpop", statuses: list | None = None,
            view_uri: str = "", cap: _ByteCap | None = None, view_tool: ViewTool | None = None):
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async def record(response):
        statuses.append(response.status_code)

    def factory(headers=None, timeout=None, auth=None, **_kw):
        kwargs = {
            "headers": headers, "auth": auth,
            # Never: a redirect would carry a bound token to a URL its proof
            # was not made for, and is the cheapest way around the host check.
            "follow_redirects": False,
            "timeout": timeout or httpx2.Timeout(10.0, read=60.0),
        }
        if statuses is not None:
            kwargs["event_hooks"] = {"response": [record]}
        inner = _transport_override if _transport_override is not None else httpx2.AsyncHTTPTransport()
        kwargs["transport"] = _capped_transport(inner, cap or _ByteCap())
        return httpx2.AsyncClient(**kwargs)

    transport = StreamableHttpTransport(
        ctx.resource, headers={ACTOR_HEADER: ctx.agent_slug}, auth=_auth(ctx, mode, view_uri, view_tool),
        httpx_client_factory=factory)
    # MCP Apps (SEP-1865): canopy renders a host's Views, so it says so in
    # `initialize` (§Client<>Server Capability Negotiation). A host that does not
    # declare the extension is unaffected; one that does may attach `_meta.ui`
    # to its tools and, for Labs, withhold a commit token from the model.
    return Client(transport, extensions=[_UI_ADVERTISEMENT()])


def _UI_ADVERTISEMENT():
    from mcp.client.extension import advertise

    from apps.mcp_apps_host.mcp_apps import RESOURCE_MIME, UI_EXTENSION

    return advertise(UI_EXTENSION, {"mimeTypes": [RESOURCE_MIME]})


async def _check_target(ctx: SiteContext) -> None:
    import asyncio

    from . import outbound

    if _transport_override is not None:
        return
    try:
        await asyncio.to_thread(outbound.check_url, ctx.resource, what="site's MCP server")
    except outbound.OutboundError as exc:
        raise GatewayRefusal("bad_resource", str(exc)) from exc


async def _listing(ctx: SiteContext, client) -> list:
    """The host's raw `tools/list` for this grant, with every tool's `_meta`
    folded into the site's MCP Apps index on the way past."""
    from apps.mcp_apps_host import mcp_apps

    tools = await client.list_tools()
    if ctx.app_id is not None:
        try:
            await sync_to_async(mcp_apps.record_tools, thread_sensitive=True)(
                ctx.app_id, [(t.name, getattr(t, "meta", None) or {}) for t in tools])
        except Exception:  # noqa: BLE001 - a cache write must never fail the call
            import logging

            logging.getLogger(__name__).exception("mcp_apps index write failed")
    return tools


async def list_tools(ctx: SiteContext) -> list[dict]:
    """The host's tools this turn may use: what the host lists for the
    visitor's grant, narrowed by the owner's ceiling if there is one, and never
    a tool the host marked app-only (MCP Apps §Visibility: "Host MUST NOT include
    tools in the agent's tool list when their visibility does not include
    "model""). Those exist for the host's own View, as the person looking at it."""
    from apps.mcp_apps_host import mcp_apps

    await _check_target(ctx)
    try:
        async with _client(ctx) as client:
            tools = await _listing(ctx, client)
    except GatewayRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - the type only; never the token
        raise GatewayRefusal("host_unreachable",
                             f"{ctx.site} did not answer ({type(exc).__name__})") from None
    return [{"name": t.name, "description": t.description or "",
             "input_schema": getattr(t, "input_schema", None) or t.inputSchema or {}}
            for t in tools
            if ctx.allows(t.name) and mcp_apps.model_visible(getattr(t, "meta", None) or {})]


#: How fresh the site's MCP Apps index must be for `call_tool` to trust it
#: about visibility instead of re-listing. A listing is a full `tools/list`
#: (Labs' is hundreds of tools), so every agent call paying one would double the
#: gateway's traffic for a check that changes when a host deploys.
INDEX_FRESH = timedelta(minutes=10)


async def _model_may_call(ctx: SiteContext, name: str, client) -> bool:
    """Whether the AGENT may call `name`: anything but an app-only tool.

    Visibility is not a security boundary against a direct client — the host
    still authorises every call as the visitor — but a hidden tool must not be
    callable by name either, or hiding it means nothing (spec 2026-10-08 §2).
    """
    from apps.mcp_apps_host import mcp_apps
    from apps.tokens.models import AppCredential

    app = (await sync_to_async(AppCredential.objects.filter(pk=ctx.app_id).first,
                               thread_sensitive=True)()) if ctx.app_id else None
    age = mcp_apps.index_age(app) if app is not None else None
    if age is not None and age < INDEX_FRESH:
        entry = mcp_apps.indexed_tool(app, name)
        return entry is None or mcp_apps.MODEL in (entry.get("visibility") or [])
    for tool in await _listing(ctx, client):
        if tool.name == name:
            return mcp_apps.model_visible(getattr(tool, "meta", None) or {})
    return True  # not listed: the host refuses it itself, as it always has


async def call_tool(ctx: SiteContext, tool: str, arguments: dict) -> dict:
    """Call ONE host tool as the visitor. The caller has already checked
    `ctx.allows(tool)`; checked again here so no path can skip it."""
    if not ctx.allows(tool):
        raise GatewayRefusal("not_allowed", f"{tool} is not something I may use on {ctx.site} here")
    await _check_target(ctx)
    name = host_tool_name(tool)
    cap = _ByteCap()
    try:
        async with _client(ctx, cap=cap) as client:
            if not await _model_may_call(ctx, name, client):
                raise GatewayRefusal(
                    "not_allowed",
                    f"{tool} on {ctx.site} is only for the page's own view, not something I call")
            result = await client.call_tool(name, arguments or {}, raise_on_error=False)
    except GatewayRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - the type only; never the token
        if cap.tripped:
            raise cap.refusal(ctx) from None
        raise GatewayRefusal("host_unreachable",
                             f"{ctx.site} did not answer ({type(exc).__name__})") from None
    answer = agent_answer(ctx.site, name, result)
    await sync_to_async(remember_links, thread_sensitive=True)(ctx, name, answer["links"])
    return answer


def agent_answer(site: str, name: str, result) -> dict:
    """A host's `CallToolResult` as `site_call` gives it to the agent:
    `{site, tool, is_error, links, content, structured}`.

    `links` — the result's `resource_link`s, by their MCP wire names (`mimeType`) —
    come FIRST: everything after them in a transcript row can be cut
    (`canopy_transcript.TOOL_TEXT_MAX`), and the link is what the person's View reads
    the result back through. `content` is the rest. A text block that is only the
    JSON of `structured` (MCP's backwards-compatible copy) is left out: the agent reads
    `structured`, and the facts are not carried twice."""
    links, content = [], []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            content.append({"type": "text", "text": text})
            continue
        dumped = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        (links if dumped.get("type") == "resource_link" else content).append(dumped)
    structured = result.structured_content
    if structured is not None:
        content = [b for b in content if not _is_json_of(b, structured)]
    return {"site": site, "tool": name, "is_error": bool(result.is_error),
            "links": links, "content": content, "structured": structured}


def _is_json_of(block: dict, value) -> bool:
    import json  # noqa: PLC0415 — as the rest of this module: loaded with a call

    if block.get("type") != "text" or not isinstance(block.get("text"), str):
        return False
    try:
        return json.loads(block["text"]) == value
    except ValueError:
        return False


# --- MCP Apps: a View's own calls, as the VIEWER ---------------------------------
#
# These serve the browser routes behind a rendered View (spec 2026-10-08 §4-5,
# `apps/mcp_apps_host/mcp_apps_views.py`), never the agent. `ctx` comes from
# `mcp_apps_views.resolve_for_viewer`: the grant of whoever CLICKED, not the
# turn's initiator. Results are returned whole (`content`, `structuredContent`,
# `isError`) because the View, unlike the agent, renders `structuredContent`.


def _unreachable(ctx: SiteContext, exc: Exception) -> GatewayRefusal:
    return GatewayRefusal("host_unreachable", f"{ctx.site} did not answer ({type(exc).__name__})")


async def view_call(ctx: SiteContext, name: str, arguments: dict, *, check, view_uri: str = "") -> dict:
    """List as the viewer, let `check(tool_or_None)` refuse (it raises a
    GatewayRefusal), then call — one MCP session, so the visibility decided is
    the visibility of the server the call goes to (§Visibility gate 3-4).

    `view_uri` is the card's own View (`ui://…`). When `ctx.view` says this is the
    person's own View and the listed tool belongs to that same View
    (`_meta.ui.resourceUri`), the call carries `Canopy-View` for THIS `tools/call` —
    what a host needs before it lets a card's button commit. An agent's turn never
    reaches here (`call_tool`), and its context carries no `view`."""
    await _check_target(ctx)
    cap = _ByteCap()
    target = ViewTool(name)
    try:
        async with _client(ctx, cap=cap, view_tool=target) as client:
            tools = await _listing(ctx, client)
            listed = next((t for t in tools if t.name == name), None)
            check(listed)
            ui = ((getattr(listed, "meta", None) or {}).get("ui") or {}) if listed is not None else {}
            if view_uri and str(ui.get("resourceUri") or "") == view_uri:
                target.uri = view_uri
            result = await client.call_tool(name, arguments or {}, raise_on_error=False)
    except GatewayRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - the type only; never the token
        if cap.tripped:
            raise cap.refusal(ctx) from None
        raise _unreachable(ctx, exc) from None
    out = {"content": [b.model_dump(mode="json", by_alias=True, exclude_none=True)
                       for b in result.content or []],
           "isError": bool(result.is_error)}
    if result.structured_content is not None:
        out["structuredContent"] = result.structured_content
    return out


# --- the links a turn may read back ---------------------------------------------------

#: Server-owned session metadata (`canopy_sessions.services.SERVER_OWNED_METADATA`): the
#: resource links the host's tools returned through the gateway in this conversation,
#: `[{"uri": <resource, no query>, "tool": <host tool>}]`, newest last.
LINKS_KEY = "host_links"
MAX_REMEMBERED_LINKS = 200


def link_key(uri: str) -> str:
    """A link as `site_read` matches it: the resource, any page of it (no query)."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(uri or ""))
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, "", ""))


def remember_links(ctx: SiteContext, tool: str, content: list) -> None:
    """Record the resource links `tool` returned (under a scheme the site declares) on the
    turn's conversation, so `site_read` reads back only what this conversation was given."""
    if not ctx.session_id:
        return
    from apps.canopy_sessions.models import Session  # noqa: PLC0415 — a model/core module: loads with a call, as the gateway's other model reads
    from apps.harness import sensitivity  # noqa: PLC0415 — a model/core module: loads with a call, as the gateway's other model reads

    with transaction.atomic():
        session = Session.objects.select_for_update().filter(pk=ctx.session_id).first()
        if session is None:
            return
        links = resource_links.validate_links(content, sensitivity.resource_schemes(session))
        if not links:
            return
        meta = dict(session.metadata or {})
        known = [e for e in (meta.get(LINKS_KEY) or []) if isinstance(e, dict)]
        fresh = {link_key(link["uri"]) for link in links}
        known = [e for e in known if e.get("uri") not in fresh]
        known += [{"uri": link_key(link["uri"]), "tool": host_tool_name(tool)} for link in links]
        meta[LINKS_KEY] = known[-MAX_REMEMBERED_LINKS:]
        Session.objects.filter(pk=session.pk).update(metadata=meta)


def readable_link(ctx: SiteContext, uri: str) -> None:
    """Refuse a `site_read` of anything but a link a site tool returned in THIS
    conversation, under a scheme the site declares, from a tool the owner's ceiling
    allows. Raises GatewayRefusal."""
    from apps.canopy_sessions.models import Session  # noqa: PLC0415 — a model/core module: loads with a call, as the gateway's other model reads
    from apps.harness import sensitivity  # noqa: PLC0415 — a model/core module: loads with a call, as the gateway's other model reads

    session = Session.objects.filter(pk=ctx.session_id).first() if ctx.session_id else None
    if session is None:
        raise GatewayRefusal("not_a_result", "I can read only results the site gave me in this conversation")
    scheme = (resource_links.scheme_of(uri) or "").lower()
    if not scheme or scheme not in sensitivity.resource_schemes(session):
        raise GatewayRefusal("undeclared_scheme",
                             f"{ctx.site} does not declare {scheme or 'that'}: links as its results")
    key = link_key(uri)
    entry = next((e for e in reversed((session.metadata or {}).get(LINKS_KEY) or [])
                  if isinstance(e, dict) and e.get("uri") == key), None)
    if entry is None:
        raise GatewayRefusal("not_a_result", "I can read only results the site gave me in this conversation")
    if not ctx.allows(str(entry.get("tool") or "")):
        raise GatewayRefusal("not_allowed",
                             f"that result came from {entry.get('tool')}, which I may not use on {ctx.site} here")


async def read_resource(ctx: SiteContext, uri: str) -> dict:
    """`resources/read` for the AGENT (`site_read`), as the visitor — the read twin of
    `call_tool`, with the same resolution behind it (`resolve`: the conversation's
    runner requirements, a capability naming the site, the visitor's live grant) and
    this turn's `Canopy-Execution` statement on every request. Only a link a site tool
    returned in this conversation, under a scheme the site declares, from a tool inside
    the owner's ceiling (`readable_link`); the host's own ACL then decides what the
    visitor may read. The answer is returned, never kept or audited."""
    uri = str(uri or "").strip()
    if not uri or len(uri) > 2048 or ":" not in uri:
        raise GatewayRefusal("bad_uri", "that is not a resource URI")
    await sync_to_async(readable_link, thread_sensitive=True)(ctx, uri)
    contents = await view_read(ctx, uri)
    return {"site": ctx.site, "uri": uri, "contents": contents}


async def view_read(ctx: SiteContext, uri: str) -> list[dict]:
    """`resources/read` as the viewer: each content as `{uri, mimeType, text|blob, _meta}`."""
    from mcp.shared.exceptions import MCPError as McpError  # noqa: PLC0415 — as probe_call: the MCP client loads with a call

    await _check_target(ctx)
    cap = _ByteCap()
    try:
        # A View's read carries `Canopy-View` for THIS uri when `ctx.view` says the person
        # is looking; an agent's (`read_resource`) carries its `Canopy-Execution` instead.
        async with _client(ctx, view_uri=uri, cap=cap) as client:
            contents = await client.read_resource(uri)
    except Exception as exc:  # noqa: BLE001 — sorted below
        if cap.tripped:
            raise cap.refusal(ctx) from None
        if not isinstance(exc, McpError):
            raise _unreachable(ctx, exc) from None
        # The host answered and said no (an artifact expired, not this person's):
        # its sentence, never "did not answer".
        detail = str(getattr(exc, "message", "") or getattr(getattr(exc, "error", None), "message", "")
                     or exc)[:300]
        raise GatewayRefusal("host_refused", f"{ctx.site} refused: {detail}") from None
    return [c.model_dump(mode="json", by_alias=True, exclude_none=True) for c in contents or []]


async def view_list(ctx: SiteContext) -> list[tuple[str, dict]]:
    """`tools/list` as `ctx`'s grant, folded into the index — `(name, _meta)`."""
    await _check_target(ctx)
    try:
        async with _client(ctx) as client:
            tools = await _listing(ctx, client)
    except Exception as exc:  # noqa: BLE001
        raise _unreachable(ctx, exc) from None
    return [(t.name, getattr(t, "meta", None) or {}) for t in tools]


# --- the live probe's view of a call -------------------------------------------


@dataclass
class CallOutcome:
    """How ONE call ended, for the live probe — which must tell "the host
    refused" apart from "the host is down", something `call_tool` (which only
    has to say "that did not work" to an agent) never needed to.

    * ``ok`` — the tool ran and did not flag an error;
    * ``tool_error`` — the host answered, and the tool (or the host's scope
      check) refused: a JSON-RPC error or ``isError``;
    * ``unauthorized`` — the host refused the credential (HTTP 401/403);
    * ``server_error`` — the host answered 5xx: it FAILED, it did not refuse;
    * ``unreachable`` — anything else (transport, a malformed answer).

    The HTTP status outranks the MCP client's own exception: the client turns a
    401 AND a 500 into the same McpError ("-32603 Server returned an error
    response"), so classifying by the exception made a crashing host read as a
    host that correctly refused — the live probe's DPoP check passed on it
    (found 2026-09-28, the first time the probe ran on labs).
    """

    kind: str
    detail: str = ""
    statuses: list = field(default_factory=list)


async def probe_call(ctx: SiteContext, tool: str, arguments: dict, *, mode: str = "dpop") -> CallOutcome:
    """Call ONE tool for the live probe, through the same client every visitor's
    call uses, with the proof `mode` chooses. Never raises for the host's
    answer; the outcome says what happened. The token never reaches `detail`."""
    from mcp.shared.exceptions import MCPError as McpError

    await _check_target(ctx)
    statuses: list[int] = []
    name = host_tool_name(tool)
    try:
        async with _client(ctx, mode=mode, statuses=statuses) as client:
            result = await client.call_tool(name, arguments or {}, raise_on_error=False)
    except McpError as exc:
        return _classify_by_status(statuses) or CallOutcome(
            "tool_error", str(getattr(exc, "error", exc))[:200], statuses)
    except Exception as exc:  # noqa: BLE001 - classified by what the host answered
        return _classify_by_status(statuses) or CallOutcome("unreachable", type(exc).__name__, statuses)
    if result.is_error:
        text = " ".join(getattr(b, "text", "") or "" for b in (result.content or []))
        return CallOutcome("tool_error", text[:200], statuses)
    return CallOutcome("ok", "", statuses)


def _classify_by_status(statuses: list[int]) -> CallOutcome | None:
    """The outcome an HTTP status settles on its own, or None. A refusal of the
    credential (401/403) is what the probe WANTS to see without a valid proof;
    a 5xx is a host failure and must never be mistaken for one."""
    refused = [s for s in statuses if s in (401, 403)]
    if refused:
        return CallOutcome("unauthorized", f"HTTP {refused[0]}", statuses)
    failed = [s for s in statuses if s >= 500]
    if failed:
        return CallOutcome("server_error", f"HTTP {failed[0]}", statuses)
    return None
