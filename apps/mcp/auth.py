"""Per-user PAT authentication for the MCP server.

`CanopyPATVerifier` is a FastMCP `TokenVerifier` that resolves a raw
bearer token to a canopy-web Django user via the existing PAT store
(`apps.tokens.models.PersonalToken`). This mirrors
`apps.tokens.middleware.BearerTokenAuthMiddleware` exactly so a PAT works
identically against the REST surface and the MCP surface.

On a hit, `verify_token` returns a FastMCP `AccessToken` whose claims
carry the user's id and email (`sub` = user id) so tools can recover the
authenticated user with `get_access_token()`. On a miss it returns
`None`, which FastMCP turns into a 401.

ORM access is wrapped in `sync_to_async` because `verify_token` runs in
the MCP server's async event loop while `PersonalToken.lookup` /
`.touch` hit the database synchronously.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from fastmcp.server.auth import AccessToken, TokenVerifier

# Scope advertised for PAT-authenticated callers. PATs are full-user
# tokens (they act as the user), so a single coarse scope is sufficient;
# per-tool authorization is enforced in the tool bodies / rate limiter.
PAT_SCOPES = ["canopy:user"]


def _lookup_user(raw: str):
    """Synchronous PAT lookup. Returns (user, token) or (None, None)."""
    from apps.tokens.models import PersonalToken

    token = PersonalToken.lookup(raw)
    if token is None:
        return None, None
    # Stamp last_used_at, mirroring BearerTokenAuthMiddleware.
    from django.utils import timezone

    PersonalToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token.user, token


#: Scope for a CONFINED session's caller token (harness.caller_tokens): tools run
#: as the caller, limited to the capability — see apps/mcp/turn_scope.py.
TURN_SCOPES = ["canopy:turn"]


def _lookup_turn_grant(raw: str):
    from apps.harness.caller_tokens import resolve

    return resolve(raw)


class CanopyPATVerifier(TokenVerifier):
    """Resolve a canopy-web Personal Access Token — or a confined session's
    caller token — to an AccessToken."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
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
                        "turn_ids": sorted(grant.turn_ids), "tool_globs": grant.tool_globs},
            )
        user, _pat = await sync_to_async(_lookup_user, thread_sensitive=True)(token)
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
            },
        )
