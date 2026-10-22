"""Django Ninja v2 router for the /api/tokens/ surface."""
from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, ProblemError

from .models import OAuthGrant, PersonalToken
from .schemas import (
    McpClientOut,
    McpClientsOut,
    PersonalTokenCreatedOut,
    PersonalTokenCreateIn,
    PersonalTokenOut,
)

router = Router(auth=session_auth, tags=["tokens"])


def _serialize(token: PersonalToken) -> dict:
    return {
        "id": token.pk,
        "label": token.label,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "last_used_client": token.last_used_client,
        "revoked_at": token.revoked_at,
        "expires_at": token.expires_at,
    }


@router.get("/", response=list[PersonalTokenOut], summary="List my tokens")
def list_tokens(request: HttpRequest) -> list[PersonalTokenOut]:
    # Tokens an MCP login minted are listed under their connected app instead:
    # they live an hour and are replaced on every refresh.
    qs = (PersonalToken.objects.filter(user=request.user, oauth_grant__isnull=True)
          .order_by("-created_at"))
    return [PersonalTokenOut.model_validate(_serialize(t)) for t in qs]


@router.post("/", response={201: PersonalTokenCreatedOut}, summary="Mint a token")
def create_token(request: HttpRequest, payload: PersonalTokenCreateIn) -> Status:
    # An hour-long OAuth token (or a site's delegated token) minting a PAT that
    # never expires and survives "Disconnect" is the escalation this refuses.
    from .middleware import minting_refusal

    refused = minting_refusal(request)
    if refused:
        raise ProblemError(403, "Cannot mint a token with this credential",
                           type_=TYPE_FORBIDDEN, detail=refused)
    raw, token = PersonalToken.create_for_user(
        user=request.user, label=payload.label, ttl_days=payload.ttl_days
    )
    body = _serialize(token) | {"raw": raw}
    return Status(201, PersonalTokenCreatedOut.model_validate(body))


@router.delete("/{pk}/", response={204: None}, summary="Revoke a token (mine only)")
def revoke_token(request: HttpRequest, pk: int) -> Status:
    token = PersonalToken.objects.filter(pk=pk, user=request.user).first()
    if token is None:
        raise ProblemError(404, "Token not found", type_=TYPE_NOT_FOUND)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return Status(204, None)


# ---- MCP clients connected by signing in (apps/tokens/mcp_oauth.py) ----------


@router.get("/connected-apps", response=McpClientsOut,
            summary="MCP clients connected to my account")
def list_connected_apps(request: HttpRequest) -> McpClientsOut:
    """The apps (Claude, an editor, …) you signed in to canopy's MCP server
    from, and the address to give a new one."""
    from . import mcp_oauth
    from .views_mcp_oauth import mcp_resource

    apps = [
        McpClientOut(id=g.pk, client_name=g.client.display_name,
                        connected_at=g.created_at, last_used_at=g.last_used_at)
        for g in mcp_oauth.grants_for(request.user)
    ]
    return McpClientsOut(mcp_url=mcp_resource(request), apps=apps)


@router.delete("/connected-apps/{grant_id}", response={204: None},
               summary="Disconnect an MCP client")
def disconnect_app(request: HttpRequest, grant_id: int) -> Status:
    """Revoke the app's access at once: its current token stops working and it
    cannot refresh. It must be signed in again to reconnect."""
    grant = OAuthGrant.objects.filter(pk=grant_id, user=request.user,
                                      revoked_at__isnull=True).first()
    if grant is None:
        raise ProblemError(404, "Connected app not found", type_=TYPE_NOT_FOUND)
    grant.revoke()
    return Status(204, None)
