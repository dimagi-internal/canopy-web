"""The embedded widget's own surface: what may this host offer, to this user.

One route so far. It answers the picker's question, which is a three-way one —
**agent x host x user** — and which nothing could answer before: `AppCredential`
covered app x tenant, `Agent.workspace` covered agent x tenant, and the app↔agent
edge did not exist. So a host named its single agent in its own settings
(ace-web's `CANOPY_AGENT_SLUG`) and canopy had no record of the choice.

Mounted under `/api/embed` rather than on the agents router (spec §12 left this
open) because the resource here is not an agent — it is *this embedding app's
offer to this caller*. Hanging it off `/api/agents` would read as a filtered
agent index, and the next person would reasonably add `?app=` to it, which is
precisely the parameter that must not exist.
"""

from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.workspaces import services as wsvc

from .schemas import EmbedAgentOut

embed_router = Router(auth=session_auth, tags=["embed"])


def _acting_app(request: HttpRequest):
    """The `AppCredential` whose delegated token authenticated this request.

    Stamped by `BearerTokenAuthMiddleware`, so it can only come from the token —
    never a path, query or body the caller controls. That is what stops one host
    enumerating (or borrowing) another host's allowlist.

    Absent on every other auth path. A browser session has no app behind it, and
    the honest answer there is a refusal: falling back to "all agents this user
    can see" would silently turn this into an unscoped agent index, which is the
    opposite of what it exists to express.
    """
    app = getattr(request, "delegated_app", None)
    if app is None:
        raise HttpError(403, "this endpoint answers for an embedding app; present a delegated token")
    if app.revoked_at is not None:
        # `AppCredential.lookup` already refuses a revoked credential at
        # exchange, so this covers the window where a token outlives the
        # revocation of the app that minted it.
        raise HttpError(403, "this embedding app's credential has been revoked")
    return app


@embed_router.get("/agents", response=list[EmbedAgentOut],
                  summary="Agents this embedding app may offer to this user")
def list_embeddable_agents(request: HttpRequest) -> list[EmbedAgentOut]:
    """The INTERSECTION of two independent grants, both required:

    1. an `AppCredentialAgent` row — an admin allowed this app to offer the
       agent;
    2. membership of the agent's tenant — the caller can actually reach it.

    Neither is sufficient. (1) alone would let a host offer an agent to someone
    with no access to it; (2) alone is just "every agent you can see", which
    ignores what the host was permitted to embed.

    Fails closed in the ordinary way: an app with no rows offers nothing, the
    same way an empty `allowed_delegation_domains` vouches for nobody.
    """
    app = _acting_app(request)
    reachable = wsvc.user_workspace_slugs(request.user)
    rows = (
        Agent.objects.filter(embedding_apps__app=app, workspace_id__in=reachable)
        .order_by("slug")
        .distinct()
    )
    return [
        EmbedAgentOut(
            slug=a.slug,
            name=a.name,
            description=a.description,
            avatar_url=a.avatar_url,
            workspace=a.workspace_id,
        )
        for a in rows
    ]
