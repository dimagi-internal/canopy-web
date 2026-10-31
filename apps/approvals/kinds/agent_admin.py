"""Kind: make a workspace member an admin of an agent.

An agent admin can run the agent and change its settings, and granting it hands over the agent's
credentials, so it is held to the bar of transferring the agent: the agent's owner or a workspace
owner (the same rule as `PUT /api/agents/{slug}/admins/{user_id}`).
"""
from __future__ import annotations

from apps.agents.models import Agent, AgentAdmin
from apps.common.schemas import StrictModel
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import Described, ExecError, Kind, KindError, register, user_by_email
from .workspace_members import _email


class Params(StrictModel):
    agent: str
    email: str


def _agent(slug: str) -> Agent:
    agent = Agent.objects.filter(slug=slug).select_related("workspace").first()
    if agent is None:
        raise KindError(f"there is no agent {slug!r}")
    return agent


@register
class AgentAdminAdd(Kind):
    slug = "agent.admin.add"
    label = "Make someone an admin of an agent"
    params_model = Params
    needs = "the agent's owner, or an owner of its workspace"
    does = ("Adds the person as an admin of the agent. They must already be a member of the agent's "
            "workspace. Admins can run the agent, change its settings and reach its credentials.")

    def describe(self, p: Params) -> Described:
        agent = _agent(p.agent)
        email = _email(p.email)
        name = agent.name or agent.slug
        return Described(
            workspace=agent.workspace_id,
            title=f"Make {email} an admin of {name}",
            summary=f"{email} would be able to run {name}, change its settings and reach its credentials.",
            plan=[f"{email} becomes an admin of the agent {name} ({agent.workspace_id})",
                  "They must already be a member of that workspace"],
            scopes=[f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"], about_email=email)

    def may_approve(self, user, p: Params, d: Described) -> bool:
        agent = _agent(p.agent)
        if (user.email or "").lower() == d.about_email:
            return False
        return agent.owner_id == user.pk or perms.can(user, agent.workspace_id, perms.OWN)

    def execute(self, user, p: Params, request) -> dict:
        agent = _agent(p.agent)
        target = user_by_email(_email(p.email))
        if target is None or not wsvc.is_member(target, agent.workspace_id):
            raise ExecError(f"{p.email} must be a member of {agent.workspace_id} first — ask to add them "
                            "to the workspace")
        _row, created = AgentAdmin.objects.get_or_create(agent=agent, user=target,
                                                         defaults={"granted_by": user})
        return {"agent": agent.slug, "email": p.email, "already_admin": not created}
