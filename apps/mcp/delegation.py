"""A host-grant token at canopy's own MCP: run as the visitor, only in scope.

canopy-web is a host of its own MCP (`apps/tokens/self_host.py`), so `/api/mcp/`
accepts one more credential beside a PAT and a confined session's caller token:
a DPoP-bound access token canopy's own token endpoint issued for a visitor on
one of canopy's pages. Three pieces, each the SDK's, wired here:

* ``gate(app)`` — the SDK's ``DPoPGate`` in front of the MCP app. For
  ``Authorization: DPoP <token>`` it checks the proof (method, canopy's public
  MCP URL, the token's hash, freshness, single-use ``jti``), records the proving
  key's thumbprint, and hands FastMCP a plain ``Bearer``. Every other request
  passes through untouched — PATs and caller tokens see no difference.
* ``access_token_for(raw)`` — called by ``CanopyPATVerifier`` ONLY when a DPoP
  key was proved on this request. Resolves the token with the SDK's
  ``ResourceVerifier`` (known, unexpired, subject still active, bound to the key
  that proved) into an ``AccessToken`` whose ``user_id`` is the VISITOR, so every
  tool applies the visitor's own ACL — the same service functions a PAT reaches.
* ``DelegatedScopeMiddleware`` — only the tools the token's scopes map to
  (``self_host.SCOPE_TOOLS``) exist for it: listing and calling are both
  filtered, and resources and prompts are closed to it entirely.

So a member's delegated token reaches at most the intersection of their own
ACL and a read-only scope — never more than their own PAT would.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from canopy_sdk.host import DPoPGate, HostNotConfigured, presented_dpop_jkt
from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

AUTH_METHOD = "delegated"


def _run_sync(fn, *args):
    # thread_sensitive: the ORM's connections are per-thread; the same executor
    # every other MCP-side ORM call here uses.
    return sync_to_async(fn, thread_sensitive=True)(*args)


def _verifier():
    # Raises HostNotConfigured on a canopy that is not a host; the SDK's gate
    # (>= 0.3.0) answers that DPoP request 401 invalid_dpop_proof, never a 500.
    from apps.tokens import self_host

    return self_host.resource_verifier()


def gate(app):
    """Wrap canopy's MCP ASGI app. ``_verifier`` is a factory so a
    configuration change applies without a rebuild."""
    return DPoPGate(app, _verifier, run_sync=_run_sync)


def _resolve(raw: str, jkt: str):
    from apps.tokens import self_host

    try:
        return self_host.resource_verifier().resolve(raw, jkt)
    except HostNotConfigured:
        return None


async def access_token_for(raw: str) -> AccessToken | None:
    """The visitor's ``AccessToken`` for a delegated token proved on THIS
    request, else None. With no proof there is nothing to resolve: a bound token
    sent as a plain bearer is exactly as useless as an unknown one."""
    jkt = presented_dpop_jkt.get()
    if jkt is None or not raw:
        return None
    principal = await _run_sync(_resolve, raw, jkt)
    if principal is None:
        return None
    try:
        user_id = int(principal.subject)
    except (TypeError, ValueError):
        return None
    return AccessToken(
        token=raw,
        client_id=principal.client_id,
        scopes=list(principal.scopes),
        claims={
            # `sub` is a namespaced string, never a bare number, so nothing can
            # mistake the grant for the user's own login; `user_id` is who the
            # tools run as.
            "sub": f"delegated:{principal.subject}",
            "user_id": user_id,
            "auth_method": AUTH_METHOD,
            "allowed_tools": sorted(principal.allowed_tools),
            **{k: v for k, v in principal.claims().items() if k in ("act", "cnf", "exp")},
        },
    )


def _delegated_claims() -> dict | None:
    try:
        tok = get_access_token()
    except Exception:  # noqa: BLE001 - no request context
        return None
    claims = (tok.claims or {}) if tok is not None else {}
    return claims if claims.get("auth_method") == AUTH_METHOD else None


def _allowed(name: str, claims: dict) -> bool:
    return name in set(claims.get("allowed_tools") or [])


class DelegatedScopeMiddleware(Middleware):
    """A delegated token sees and calls only its scopes' tools, and nothing else."""

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        tools = await call_next(context)
        claims = _delegated_claims()
        if claims is None:
            return tools
        return [t for t in tools if _allowed(t.name, claims)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        claims = _delegated_claims()
        if claims is not None and not _allowed(context.message.name, claims):
            raise ToolError(f"{context.message.name} is not within this grant")
        return await call_next(context)

    async def on_list_resources(self, context: MiddlewareContext, call_next):
        if _delegated_claims() is not None:
            return []
        return await call_next(context)

    async def on_list_resource_templates(self, context: MiddlewareContext, call_next):
        if _delegated_claims() is not None:
            return []
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        if _delegated_claims() is not None:
            raise ResourceError("resources are not within this grant")
        return await call_next(context)

    async def on_list_prompts(self, context: MiddlewareContext, call_next):
        if _delegated_claims() is not None:
            return []
        return await call_next(context)

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        if _delegated_claims() is not None:
            raise ToolError("prompts are not within this grant")
        return await call_next(context)
