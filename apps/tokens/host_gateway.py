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
   that issues host grants; the turn's capability names that site in `sites:`;
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

**The effective set** is what three independent parties allow, intersected:

* the owner's per-site **ceiling** (`ceiling:` on the capability);
* the tools the **page** says back its view (`page_state.backing_tool`) — a
  narrowing only, since the page's JavaScript is untrusted; and
* the **host's** own scopes and ACL, enforced by the host as the visitor.

canopy enforces the first two; a page that declares no backing tool unlocks
nothing.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

#: A grant this close to expiry is treated as expired: the call would race it.
EXPIRY_MARGIN = timedelta(seconds=10)
#: Header the host logs as the acting agent. Informational: not trusted for authz.
ACTOR_HEADER = "Canopy-Actor"
BACK_ON_THE_PAGE = ("I need you back on the page to do that — ask again from the site "
                    "and I will try once more.")


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
    backing: list[str]
    _token: str = field(repr=False, default="")

    def allows(self, tool: str) -> bool:
        return tool_allowed(tool, ceiling=self.ceiling, backing=self.backing)


# --- names --------------------------------------------------------------------


def host_tool_name(name: str) -> str:
    """A tool's name AT THE HOST, from however it is written here.

    Owners write ceilings in Claude Code's naming (`mcp__*connect_labs__*`),
    and pages may name a backing tool the same way or bare
    (`marketplace_orgs_get`). The server segment is dropped: the gateway only
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


def tool_allowed(tool: str, *, ceiling: list[str], backing: list[str]) -> bool:
    """Inside the ceiling AND named by the page. Fails closed on either empty."""
    name = host_tool_name(tool)
    if not name:
        return False
    globs = [g for g in (host_tool_name(c) for c in ceiling) if g]
    if not any(fnmatch.fnmatchcase(name, g) for g in globs):
        return False
    return name in {host_tool_name(b) for b in backing}


def _backing_tools(session) -> list[str]:
    state = (getattr(session, "page_state", None) or {}) if session is not None else {}
    out: list[str] = []
    for key in ("backing_tool", "backing_tools"):
        value = state.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, list):
            out.extend(v.strip() for v in value if isinstance(v, str) and v.strip())
    return out


# --- resolution ----------------------------------------------------------------


def resolve(turn_id: str) -> SiteContext:
    """The site, grant and effective tools for this turn — or a refusal."""
    from apps.common.encryption import decrypt_secret
    from apps.harness.models import Turn

    from . import client_identity
    from .models import AppCredential, HostGrant

    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__agent",
                                        "initiator_contact", "initiator_user")
            .filter(pk=turn_id).first())
    if turn is None:
        raise GatewayRefusal("no_turn", "turn not found")
    session = turn.chat_session if turn.chat_session_id else None
    agent = turn.agent if turn.agent_id else (session.agent if session is not None else None)
    if agent is None or session is None:
        raise GatewayRefusal("no_site", "this conversation was not held on a connected site")

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

    cap = ((agent.interface or {}).get("capabilities") or {}).get(turn.capability or "")
    if not cap or site not in (cap.get("sites") or []):
        raise GatewayRefusal("site_not_in_capability",
                             f"I am not set up to use {site}'s tools in this conversation")

    # WHO ASKED, from the turn — the grant is theirs or there is none.
    grants = HostGrant.objects.filter(app=app)
    if turn.initiator_contact_id:
        grant = grants.filter(contact_id=turn.initiator_contact_id).order_by("-updated_at").first()
    elif turn.initiator_user_id:
        grant = grants.filter(user_id=turn.initiator_user_id).order_by("-updated_at").first()
    else:
        grant = None
    if grant is None:
        raise GatewayRefusal("no_grant",
                             f"{site} has not given me access on your behalf — " + BACK_ON_THE_PAGE)
    if grant.expires_at <= timezone.now() + EXPIRY_MARGIN:
        raise GatewayRefusal("expired", BACK_ON_THE_PAGE)
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
        turn_id=str(turn.pk), agent_slug=agent.slug, site=site,
        resource=app.host_mcp_resource, scope=grant.scope, expires_at=grant.expires_at,
        ceiling=list(cap.get("ceiling") or []), backing=_backing_tools(session),
        _token=decrypt_secret(grant.access_token_enc),
    )


# --- the call ------------------------------------------------------------------

#: Tests replace this with an in-process transport (an ASGI app standing in for
#: the host). Production never sets it.
_transport_override = None


def _auth(ctx: SiteContext):
    import httpx2

    from . import client_identity

    token = ctx._token

    class _DPoP(httpx2.Auth):
        """A fresh proof on EVERY request of the MCP session (initialize,
        list, call...), each bound to that request's method and URL and to the
        token (`ath`). Retries once on a `use_dpop_nonce` challenge."""

        def auth_flow(self, request):
            htu = str(request.url.copy_with(query=None, fragment=None))
            request.headers["Authorization"] = f"DPoP {token}"
            request.headers["DPoP"] = client_identity.dpop_proof(
                request.method, htu, access_token=token)
            response = yield request
            nonce = response.headers.get("DPoP-Nonce")
            if response.status_code == 401 and nonce:
                request.headers["DPoP"] = client_identity.dpop_proof(
                    request.method, htu, access_token=token, nonce=nonce)
                yield request

    return _DPoP()


def _client(ctx: SiteContext):
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    def factory(headers=None, timeout=None, auth=None, **_kw):
        kwargs = {
            "headers": headers, "auth": auth,
            # Never: a redirect would carry a bound token to a URL its proof
            # was not made for, and is the cheapest way around the host check.
            "follow_redirects": False,
            "timeout": timeout or httpx2.Timeout(10.0, read=60.0),
        }
        if _transport_override is not None:
            kwargs["transport"] = _transport_override
        return httpx2.AsyncClient(**kwargs)

    transport = StreamableHttpTransport(
        ctx.resource, headers={ACTOR_HEADER: ctx.agent_slug}, auth=_auth(ctx),
        httpx_client_factory=factory)
    return Client(transport)


async def _check_target(ctx: SiteContext) -> None:
    import asyncio

    from . import outbound

    if _transport_override is not None:
        return
    try:
        await asyncio.to_thread(outbound.check_url, ctx.resource, what="site's MCP server")
    except outbound.OutboundError as exc:
        raise GatewayRefusal("bad_resource", str(exc)) from exc


async def list_tools(ctx: SiteContext) -> list[dict]:
    """The host's tools this turn may use: what the host lists (as the
    visitor) ∩ the effective set."""
    await _check_target(ctx)
    try:
        async with _client(ctx) as client:
            tools = await client.list_tools()
    except GatewayRefusal:
        raise
    except Exception as exc:  # noqa: BLE001 - the type only; never the token
        raise GatewayRefusal("host_unreachable",
                             f"{ctx.site} did not answer ({type(exc).__name__})") from None
    return [{"name": t.name, "description": t.description or "",
             "input_schema": getattr(t, "input_schema", None) or t.inputSchema or {}}
            for t in tools if ctx.allows(t.name)]


async def call_tool(ctx: SiteContext, tool: str, arguments: dict) -> dict:
    """Call ONE host tool as the visitor. The caller has already checked
    `ctx.allows(tool)`; checked again here so no path can skip it."""
    if not ctx.allows(tool):
        raise GatewayRefusal("not_allowed", f"{tool} is not something I may use on {ctx.site} here")
    await _check_target(ctx)
    name = host_tool_name(tool)
    try:
        async with _client(ctx) as client:
            result = await client.call_tool(name, arguments or {}, raise_on_error=False)
    except Exception as exc:  # noqa: BLE001 - the type only; never the token
        raise GatewayRefusal("host_unreachable",
                             f"{ctx.site} did not answer ({type(exc).__name__})") from None
    content = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        content.append({"type": "text", "text": text} if text is not None
                       else block.model_dump(mode="json", exclude_none=True))
    return {"site": ctx.site, "tool": name, "is_error": bool(result.is_error),
            "content": content, "structured": result.structured_content}
