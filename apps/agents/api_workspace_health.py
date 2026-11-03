"""A workspace's agent-side reads: the health checklist of every agent in its tree and the
agent-to-agent topology. Served under `/api/workspaces/{slug}/…` with the rest of workspace
settings; the logic is the agents' (`health`, `topology`), so the routes live here, above the
workspace kit they use (`apps/workspaces/gates.py`)."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces.gates import tree_reader
from apps.workspaces.schemas import AgentTopologyOut, WorkspaceHealthOut, WorkspaceHealthSetupOut

from . import health, topology

router = Router(auth=session_auth, tags=["workspaces"])


@router.get("/{slug}/health", response=WorkspaceHealthOut,
            summary="What is not set up right, across this workspace and every one below it")
@gate("logs.read")  # same tree read; can_fix mirrors Agent.is_admin
def workspace_health(request: HttpRequest, slug: str, setup: bool = True) -> WorkspaceHealthOut:
    """Every agent in the tree with its setup checklist (the same steps as its own
    Settings page and `agent_setup`), its vault state (canopy-web, moving)
    and properties; every workspace with its shared vault; the runners its agents
    route to or that live in it (the runner topology's rows, plus whether each runs
    current code and its own failing checks). Each step carries the link
    that fixes it; `can_fix` says whether the viewer may follow it, else
    `owner_email` is who to send it to. With `setup=false` each agent's checklist
    and vault state, and the totals counted from them, are null: read them from
    `/health/setup` a few agents at a time. Admin and above."""
    root, visible = tree_reader(request, slug)
    return WorkspaceHealthOut(**health.build(root, request.user, visible, setup=setup))


#: How many agents one `/health/setup` read may name.
HEALTH_SETUP_MAX = 25


@router.get("/{slug}/health/setup", response=WorkspaceHealthSetupOut,
            summary="The setup checklist and vault state of some of the tree's agents")
@gate("logs.read")  # same tree read as /health
def workspace_health_setup(request: HttpRequest, slug: str, agents: str) -> WorkspaceHealthSetupOut:
    """The `steps`, `vault` and `left` of each named agent (comma-separated slugs,
    at most 25) in this workspace's tree, as `/health` reports them. A slug outside
    the tree is left out. Admin and above."""
    # The costly half of /health: the page reads the table with setup=false, then
    # this a few agents at a time, so it shows before every checklist is computed.
    wanted = {s.strip() for s in agents.split(",") if s.strip()}
    if len(wanted) > HEALTH_SETUP_MAX:
        raise HttpError(422, f"At most {HEALTH_SETUP_MAX} agents per read")
    root, visible = tree_reader(request, slug)
    return WorkspaceHealthSetupOut(agents=health.setup_of(root, request.user, visible, wanted))



@router.get("/{slug}/agent-topology", response=AgentTopologyOut,
            summary="Which agents can send which other agents work, across this workspace and every one below it")
@gate("logs.read")  # same; grant/revoke flags mirror _may_manage_admins
def agent_topology(request: HttpRequest, slug: str) -> AgentTopologyOut:
    """Every agent in this workspace and its descendants, and for each ordered
    pair what the first gets when it sends the second work with its own canopy
    login: the whole agent, some of its capabilities, or nothing — and why.
    Admin and above. Granting access is `PUT /api/agents/{slug}/admins/{user_id}`
    with the source agent's `login_user_id`."""
    # Same gate and per-descendant filter as runner_topology.
    root, visible = tree_reader(request, slug)
    return AgentTopologyOut(**topology.build(root, request.user, visible=visible))

