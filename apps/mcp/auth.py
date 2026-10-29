"""Per-user PAT authentication for the MCP server.

`CanopyPATVerifier` is a FastMCP `TokenVerifier` that resolves a raw
bearer token to a canopy-web Django user through `apps.tokens.principal.resolve_bearer`,
the resolver `BearerTokenAuthMiddleware` and the WebSocket handshake use too, so
a PAT works — and is stamped as used — identically on every surface.

On a hit, `verify_token` returns a FastMCP `AccessToken` whose claims
carry the user's id and email (`sub` = user id) so tools can recover the
authenticated user with `get_access_token()`. On a miss it returns
`None`, which FastMCP turns into a 401.

ORM access is wrapped in `sync_to_async` because `verify_token` runs in
the MCP server's async event loop while the lookup and its use stamp hit the
database synchronously.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from fastmcp.server.auth import AccessToken, TokenVerifier

# Scope advertised for PAT-authenticated callers. PATs are full-user
# tokens (they act as the user), so a single coarse scope is sufficient;
# per-tool authorization is enforced in the tool bodies / rate limiter.
PAT_SCOPES = ["canopy:user"]


def _lookup_user(raw: str):
    """A PAT (or OAuth access token) through the one resolver every door uses
    (`apps/tokens/principal.py`) — stamped exactly as REST stamps it. Returns
    (user, token) or (None, None)."""
    from apps.tokens import principal as P
    from apps.tokens.models import client_label

    found = P.resolve_bearer(raw, accept={P.PAT}, client=client_label(_user_agent(), "mcp"))
    if found is None:
        return None, None
    return found.user, found.token


def _user_agent() -> str:
    """The MCP request's User-Agent, when there is one in scope."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        return (get_http_headers() or {}).get("user-agent", "")
    except Exception:  # noqa: BLE001 — no request in scope (a direct call, a test)
        return ""


#: Scope for a CONFINED session's caller token (harness.caller_tokens): tools run
#: as the caller, limited to the capability — see apps/mcp/turn_scope.py.
TURN_SCOPES = ["canopy:turn"]


#: Scope for an act-as-you token: the person, within `harness.act_as`'s bounds.
ACT_AS_SCOPES = ["canopy:act-as"]


def _lookup_act_as(raw: str):
    from apps.harness import act_as  # noqa: PLC0415 — only an act-as token pays for it

    grant, _why = act_as.resolve(raw)
    if grant is not None:
        act_as.touch(grant)
    return grant


def _lookup_turn_grant(raw: str):
    from apps.harness.caller_tokens import resolve

    return resolve(raw)


class CanopyPATVerifier(TokenVerifier):
    """Resolve a canopy-web Personal Access Token — or a confined session's
    caller token — to an AccessToken."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        # A DPoP key was proved on this request (the gate in front of the MCP
        # app checked it): the ONLY credential that comes with one is a
        # host-grant token canopy issued for a visitor on its own pages. It runs
        # as that visitor, limited to its scopes (apps/mcp/delegation.py). PATs
        # and caller tokens never arrive this way, so this branch cannot change
        # what they do.
        from canopy_sdk.host import presented_dpop_jkt

        if presented_dpop_jkt.get() is not None:
            from .delegation import access_token_for

            return await access_token_for(token)
        if token.startswith("cact_"):
            # An act-as-you token (`apps/harness/act_as.py`): the PERSON who approved it
            # with their passkey, re-checked on every request. What it may call is
            # `act_as_scope.ActAsScopeMiddleware`'s, and again the in-process route's.
            grant = await sync_to_async(_lookup_act_as, thread_sensitive=True)(token)
            if grant is None:
                return None
            from apps.harness import act_as  # noqa: PLC0415 — only an act-as token pays for it

            cred = act_as.credential(grant)
            return AccessToken(
                token=token, client_id=f"act-as:{grant.pk}", scopes=ACT_AS_SCOPES,
                claims={"sub": str(grant.person_id), "user_id": grant.person_id,
                        "email": grant.person.email, "auth_method": cred["type"],
                        "credential_type": cred["type"], "token_id": cred["id"],
                        "token_label": cred["label"], "turn_id": cred["turn_id"],
                        "act_as_grant": cred["act_as_grant"], "acting_agent": cred["acting_agent"],
                        "act_as_scope": cred["act_as_scope"]},
            )
        if token.startswith("cct_"):
            grant = await sync_to_async(_lookup_turn_grant, thread_sensitive=True)(token)
            if grant is None:
                return None
            # `user_id` is the CALLER — their own ACL applies — or absent for an
            # outside contact, who has no canopy account and so reaches nothing
            # that needs one. `sub` is deliberately not a number, so nothing can
            # fall back to reading it as a user id.
            return AccessToken(
                token=token, client_id=f"turn:{grant.turn.pk}", scopes=TURN_SCOPES,
                claims={"sub": f"turn:{grant.turn.pk}",
                        "user_id": grant.user.pk if grant.user is not None else None,
                        "auth_method": "caller_token", "turn_id": str(grant.turn.pk),
                        # Provenance: WHICH credential (apps/harness/provenance.py).
                        "token_id": grant.token_id, "token_label": f"turn:{grant.turn.pk}",
                        "turn_ids": sorted(grant.turn_ids), "tool_globs": grant.tool_globs,
                        "all_tools": grant.all_tools, "sensitive": grant.sensitive,
                        "read_only": grant.read_only},
            )
        user, pat = await sync_to_async(_lookup_user, thread_sensitive=True)(token)
        if user is None:
            return None

        return AccessToken(
            token=token,
            client_id=str(user.pk),
            scopes=PAT_SCOPES,
            claims={
                "sub": str(user.pk),
                "user_id": user.pk,
                "email": getattr(user, "email", "") or "",
                "auth_method": "pat",
                # Provenance: WHICH token, so a turn this call creates can name it
                # (apps/harness/provenance.py). An OAuth access token IS a
                # PersonalToken, minted for a grant — typed apart so the log says so.
                "token_id": pat.pk,
                "token_label": pat.label,
                "credential_type": "oauth" if pat.oauth_grant_id else "pat",
            },
        )
