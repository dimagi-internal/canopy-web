"""One member's page in a workspace (/w/:workspace/members/:userId).

Lives in `contacts` (L1), not `workspaces` (L0): what a member's page shows —
the agents they operate, the chats they started — is core-layer data the kit
layer may not name (ARCHITECTURE.md § Layers inside the framework). Mounted at
`/workspaces` beside the members routes, so the address reads as one surface.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.canopy_sessions.models import Session as ChatSession
from apps.workspaces import permissions as perms
from apps.workspaces import services
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.schemas import MemberAdminDetail, MemberAgentRef, MemberProfileOut, MemberWorkspaceRef

router = Router(tags=["workspaces"], auth=session_auth)


@router.get("/{slug}/members/{user_id}/profile/", response=MemberProfileOut,
            summary="One member's profile (member-only; detail by role)")
@gate("member")
def get_member(request: HttpRequest, slug: str, user_id: int) -> MemberProfileOut:
    """One member as the caller may see them. Any member sees the basics; a
    caller who manages members also gets `admin` (who invited them, last
    sign-in, how many chats they started here). 404 for a non-member caller
    and for a user who is not in this workspace alike."""
    m = services.membership(request.user, slug)
    if m is None:
        raise HttpError(404, f"workspace '{slug}' not found")
    ws = m.workspace
    row = next((r for r in services.effective_memberships(ws) if r.user_id == user_id), None)
    if row is None:
        raise HttpError(404, f"no member {user_id} in workspace '{slug}'")
    user = row.user

    own_agent = Agent.objects.filter(user_id=user_id, workspace=ws).first()
    kind = "agent" if own_agent else "person"

    operates = [MemberAgentRef(slug=a.slug, name=a.name)
                for a in Agent.objects.filter(owner_id=user_id, workspace=ws).order_by("name")]

    mine = {r.workspace_id for r in WorkspaceMembership.objects.filter(user=request.user)}
    shared = [
        MemberWorkspaceRef(slug=r.workspace.slug, display_name=r.workspace.display_name or r.workspace.slug,
                           role=r.role)
        for r in WorkspaceMembership.objects.filter(user_id=user_id, workspace_id__in=mine)
        .exclude(workspace=ws).select_related("workspace").order_by("workspace__slug")
    ]

    admin = None
    if perms.role_allows(m.role, perms.MEMBERS_MANAGE):
        invited_by = getattr(row, "invited_by", None) if not getattr(row, "inherited", False) else None
        admin = MemberAdminDetail(
            invited_by_email=invited_by.email if invited_by else None,
            last_login=user.last_login,
            chats_started=ChatSession.objects.filter(workspace=ws, created_by_id=user_id).count(),
        )

    return MemberProfileOut(
        user_id=user_id, email=user.email, name=user.get_full_name(), role=row.role,
        joined_at=row.joined_at, inherited=getattr(row, "inherited", False), kind=kind,
        is_you=user_id == request.user.id,
        agent=MemberAgentRef(slug=own_agent.slug, name=own_agent.name) if own_agent else None,
        operates=operates, shared_workspaces=shared, admin=admin,
    )
