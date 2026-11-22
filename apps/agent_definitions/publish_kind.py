"""Kind: publish a draft an agent wrote of a stored definition, when no agent may publish it.

An agent may propose changes to itself (a draft), never make them live: what runs with
its vault and credentials is decided by a person who administers it. The same holds for
a draft an agent wrote of an agent it does not administer. A draft another agent wrote as
an admin of the agent asks for nothing: that agent publishes it itself
(`harness.acting_person.admin_agent`). The request names
the exact draft by number AND root sha256, so what is approved is what goes live; a draft
replaced or discarded since makes the request invalid rather than publishing something
else.
"""
from __future__ import annotations

from pydantic import Field

from apps.agents.models import Agent, AgentDefinitionRevision
from apps.approvals.kinds import Described, ExecError, Kind, KindError, register
from apps.approvals.kinds.agent_admin import agent_recipients
from apps.common.schemas import StrictModel
from apps.workspaces.links import scoped_url

SLUG = "agent.definition.publish"


class Params(StrictModel):
    agent: str
    revision: int = Field(ge=1)
    root_sha256: str = Field(min_length=64, max_length=64)


def _is_agent_login(user) -> bool:
    return Agent.objects.filter(user_id=getattr(user, "pk", None)).exists()


@register
class AgentDefinitionPublish(Kind):
    slug = SLUG
    label = "Publish changes an agent proposed to an agent's definition"
    params_model = Params
    needs = "the agent's owner or one of its admins (a person, never an agent)"
    does = ("Publishes the agent's draft: its files become what every runner of the agent runs, with "
            "its vault and credentials. The exact draft is named by its sha256.")
    #: Approved only from the request's page with a passkey: never a Slack or alert button,
    #: and never a token — a turn acting as an admin must not be the one saying yes.
    step_up = True

    def _target(self, p: Params) -> tuple[Agent, AgentDefinitionRevision]:
        agent = Agent.objects.filter(slug=p.agent).select_related("workspace").first()
        if agent is None:
            raise KindError(f"there is no agent {p.agent!r}")
        rev = AgentDefinitionRevision.objects.filter(agent=agent, number=p.revision).first()
        if rev is None or rev.status != AgentDefinitionRevision.DRAFT or rev.root_sha256 != p.root_sha256:
            raise KindError(f"revision {p.revision} of {agent.slug} is no longer that draft")
        return agent, rev

    def describe(self, p: Params) -> Described:
        from . import services  # noqa: PLC0415 — services imports this module

        agent, rev = self._target(p)
        name = agent.name or agent.slug
        writer = rev.author_user and Agent.objects.filter(user_id=rev.author_user_id).exclude(pk=agent.pk).first()
        changed = services.diff(rev, agent.live_revision) if agent.live_revision_id else []
        plan = [f"{c['status']}: {c['path']}" for c in changed[:40]]
        if len(changed) > 40:
            plan.append(f"… and {len(changed) - 40} more files")
        plan.append(f"Exact draft: revision {rev.number}, sha256 {rev.root_sha256}")
        plan.append("Read every change: "
                    + scoped_url(agent.workspace, f"/agents/{agent.slug}/definition?revision={rev.number}"))
        return Described(
            workspace=agent.workspace_id,
            title=(f"Publish {writer.slug}'s change to {name}'s definition (revision {rev.number})" if writer
                   else f"Publish {name}'s own change to its definition (revision {rev.number})"),
            summary=(rev.message or (f"{writer.slug} proposed a change to {name}'s definition." if writer
                                     else f"{name} proposed a change to its own definition."))[:1000],
            plan=plan,
            scopes=[f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"])

    def may_approve(self, user, p: Params, d: Described) -> bool:
        if not getattr(user, "is_authenticated", False) or _is_agent_login(user):
            return False
        agent = Agent.objects.filter(slug=p.agent).first()
        return agent is not None and agent.is_admin(user)

    def recipients(self, p: Params, d: Described) -> list:
        agent = Agent.objects.filter(slug=p.agent).first()
        return agent_recipients(agent, self.approvers(p, d)) if agent else []

    def execute(self, user, p: Params, request) -> dict:
        from . import services  # noqa: PLC0415 — services imports this module

        agent, rev = self._target(p)
        try:
            rev = services.publish(agent, rev.number, by=user, expect_root=p.root_sha256, by_approval=True)
        except services.Refused as exc:
            raise ExecError(str(exc)) from exc
        return {"agent": agent.slug, "revision": rev.number, "root_sha256": rev.root_sha256}


def request(agent: Agent, rev: AgentDefinitionRevision, *, by, reason: str = ""):
    """File the request for an agent's own draft; returns the ApprovalRequest."""
    from apps.approvals import services as approvals  # noqa: PLC0415 — approvals.services loads this kind's registry

    from .services import Refused  # noqa: PLC0415 — services imports this module

    requester = Agent.objects.filter(user_id=getattr(by, "pk", None)).first() or agent
    try:
        req, _ = approvals.create(
            kind=SLUG, params={"agent": agent.slug, "revision": rev.number, "root_sha256": rev.root_sha256},
            requested_by_agent=requester, reason=reason or "",
            idempotency_key=f"agent-definition-{agent.pk}-{rev.number}-{rev.root_sha256[:16]}")
    except approvals.ApprovalError as exc:
        raise Refused(f"the draft needs a person to publish it, and canopy could not ask one: {exc}",
                      exc.status, code="no_approver") from exc
    return req
