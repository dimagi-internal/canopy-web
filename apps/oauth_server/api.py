"""The MCP clients a person connected by signing in, under `/api/tokens/connected-apps`.

Mounted on `/tokens` with the `tokens` tag, where these routes were published.
Their view names are shared with `apps/embedding/connected_apps_api.py`, so their
operationIds are module-qualified; `OPERATION_LABEL` keeps the published
`tokens_` label (the MCP tool names `tokens_list_connected_apps`,
`tokens_disconnect_app`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError
from apps.api.route_gates import gate

from .models import OAuthGrant
from .schemas import McpClientOut, McpClientsOut

OPERATION_LABEL = "tokens"

router = Router(auth=session_auth, tags=["tokens"])


# ---- MCP clients connected by signing in (mcp_oauth.py) ----------


@router.get("/connected-apps", response=McpClientsOut,
            summary="MCP clients connected to my account")
@gate("self")
def list_connected_apps(request: HttpRequest) -> McpClientsOut:
    """The apps (Claude, an editor, …) you signed in to canopy's MCP server
    from, and the address to give a new one."""
    from . import mcp_oauth
    from .views import mcp_resource

    apps = [
        McpClientOut(id=g.pk, client_name=g.client.display_name,
                        connected_at=g.created_at, last_used_at=g.last_used_at)
        for g in mcp_oauth.grants_for(request.user)
    ]
    return McpClientsOut(mcp_url=mcp_resource(request), apps=apps)


@router.delete("/connected-apps/{grant_id}", response={204: None},
               summary="Disconnect an MCP client")
@gate("self", setting=True)
def disconnect_app(request: HttpRequest, grant_id: int) -> Status:
    """Revoke the app's access at once: its current token stops working and it
    cannot refresh. It must be signed in again to reconnect."""
    grant = OAuthGrant.objects.filter(pk=grant_id, user=request.user,
                                      revoked_at__isnull=True).first()
    if grant is None:
        raise ProblemError(404, "Connected app not found", type_=TYPE_NOT_FOUND)
    grant.revoke()
    return Status(204, None)
