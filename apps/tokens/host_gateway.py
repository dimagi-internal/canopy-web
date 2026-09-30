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

from canopy_sdk import consumer, contract
from django.utils import timezone

#: A grant this close to expiry is treated as expired: the call would race it.
EXPIRY_MARGIN = timedelta(seconds=10)
#: Header the host logs as the acting agent. Informational: not trusted for authz.
#: The SDK's constant — the host's side reads the same one.
ACTOR_HEADER = contract.ACTOR_HEADER
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
    _token: str = field(repr=False, default="")
    #: The Connected site's row, so an audit line names the site unambiguously
    #: (a site NAME is unique only per tenant).
    app_id: int | None = None

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


def resolve(turn_id: str) -> SiteContext:
    """The site, grant and effective tools for this turn — or a refusal."""
    from apps.harness.models import Turn

    from .models import AppCredential, HostGrant

    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__agent",
                                        "initiator_contact", "initiator_user",
                                        "claimed_by")
            .prefetch_related("claimed_by__declared_flags")
            .filter(pk=turn_id).first())
    if turn is None:
        raise GatewayRefusal("no_turn", "turn not found")
    session = turn.chat_session if turn.chat_session_id else None
    agent = turn.agent if turn.agent_id else (session.agent if session is not None else None)
    if agent is None or session is None:
        raise GatewayRefusal("no_site", "this conversation was not held on a connected site")

    from apps.harness import runner_requirements as rr

    # Defence in depth: routing should make this unreachable. This is where the
    # host's data enters a turn, so a routing bug fails as a refusal, not a leak.
    reqs = rr.requirements_of_session(session)
    if reqs and (turn.claimed_by is None or not rr.satisfies(turn.claimed_by.flags, reqs)):
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

    # The grant is per (site, visitor), not per conversation: a visitor who
    # arrived under ZDR can reach it from an OLDER conversation that no ZDR
    # arrival stamped. So the grant's own requirements count too, and they are
    # written onto the conversation — refused or not — so its next turn routes
    # to a runner that satisfies them.
    grant_reqs = rr.requirements_of_grant(grant)
    if grant_reqs - reqs:
        from apps.canopy_sessions.services import add_runner_requirements

        add_runner_requirements(session, grant_reqs)
    effective = reqs | grant_reqs
    if effective and (turn.claimed_by is None
                      or not rr.satisfies(turn.claimed_by.flags, effective)):
        raise GatewayRefusal(
            "runner_requirements",
            f"this conversation must run on a {rr.describe(effective)} runner, and this one is not")
    return context_for_grant(app, grant, turn_id=str(turn.pk), agent_slug=agent.slug,
                             ceiling=list(cap.get("ceiling") or []))


def context_for_grant(app, grant, *, turn_id: str, agent_slug: str, ceiling: list[str]) -> SiteContext:
    """A `SiteContext` for ONE stored grant — or a refusal if the grant cannot
    be used: expired, for a resource the site no longer names, or bound to a
    DPoP key canopy no longer holds. `resolve` reaches it for a visitor's turn;
    the live probe (`live_probe.py`) reaches it for the probe's own grant, so
    both are held to the same checks and call through the same client."""
    from apps.common.encryption import decrypt_secret

    from . import client_identity

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
        turn_id=turn_id, agent_slug=agent_slug, site=app.name,
        resource=app.host_mcp_resource, scope=grant.scope, expires_at=grant.expires_at,
        ceiling=list(ceiling),
        _token=decrypt_secret(grant.access_token_enc), app_id=app.pk,
    )


# --- the call ------------------------------------------------------------------

#: Tests replace this with an in-process transport (an ASGI app standing in for
#: the host). Production never sets it.
_transport_override = None


#: How a request proves possession. Only `dpop` is ever used for a real call;
#: the rest exist so the live probe can show the host REFUSES a bound token
#: without a valid proof — through this same client, not a hand-built request.
PROOF_MODES = ("dpop", "no_proof", "stranger_key", "bearer")


def _auth(ctx: SiteContext, mode: str = "dpop"):
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
            response = yield request
            nonce = response.headers.get(contract.DPOP_NONCE_HEADER)
            if mode == "dpop" and response.status_code == 401 and nonce:
                request.headers[contract.DPOP_HEADER] = client_identity.dpop_proof(
                    request.method, htu, access_token=token, nonce=nonce)
                yield request

    return _DPoP()


def _client(ctx: SiteContext, *, mode: str = "dpop", statuses: list | None = None):
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
        if _transport_override is not None:
            kwargs["transport"] = _transport_override
        return httpx2.AsyncClient(**kwargs)

    transport = StreamableHttpTransport(
        ctx.resource, headers={ACTOR_HEADER: ctx.agent_slug}, auth=_auth(ctx, mode),
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
    """The host's tools this turn may use: what the host lists for the
    visitor's grant, narrowed by the owner's ceiling if there is one."""
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
