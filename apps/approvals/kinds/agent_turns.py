"""Kind: let an agent do some work — the one kind that does run a model, and says so.

Everything else here is a hard-coded handler. This is the explicit escape hatch for work that
genuinely needs an agent: approving starts the listed turns for the agent, with the approver as
the person who authorised them. The preview shows each prompt in full, so what the agent is
told is exactly what the approver read.
"""
from __future__ import annotations

from pydantic import Field

from apps.agents.models import Agent
from apps.common.schemas import StrictModel
from apps.harness import initiator as who
from apps.harness import services as harness
from apps.harness.dispatch_marker import stamp_dispatched
from apps.workspaces import permissions as perms

from . import Described, Kind, KindError, register
from .agent_admin import agent_recipients

MAX_PROMPTS = 3


class Params(StrictModel):
    agent: str
    prompts: list[str] = Field(min_length=1, max_length=MAX_PROMPTS)


@register
class AgentRunTurns(Kind):
    slug = "agent.run_turns"
    label = "Let an agent do some work"
    params_model = Params
    needs = "anyone who can start turns for the agent (an editor of its workspace)"
    does = ("Starts one turn per prompt for the agent, as the approver. This is the only kind that runs "
            "a model; the prompts are shown in full.")

    def _agent(self, slug: str) -> Agent:
        agent = Agent.objects.filter(slug=slug).first()
        if agent is None:
            raise KindError(f"there is no agent {slug!r}")
        return agent

    def describe(self, p: Params) -> Described:
        agent = self._agent(p.agent)
        name = agent.name or agent.slug
        prompts = [s.strip() for s in p.prompts if s.strip()]
        if not prompts:
            raise KindError("prompts must not be empty")
        first = prompts[0].splitlines()[0][:80]
        return Described(
            workspace=agent.workspace_id,
            title=f"Let {name} do: {first}",
            summary=f"{name} would start {len(prompts)} turn(s) with the prompts below.",
            plan=[f"Start a turn for {name}: {s[:600]}" for s in prompts],
            scopes=[f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"])

    def may_approve(self, user, p: Params, d: Described) -> bool:
        return perms.can(user, d.workspace, perms.AGENT_WORK)

    def recipients(self, p: Params, d: Described) -> list:
        # Anyone who can start the agent may approve, but only its owner and admins are TOLD.
        return agent_recipients(self._agent(p.agent), self.approvers(p, d))

    def execute(self, user, p: Params, request) -> dict:
        agent = self._agent(p.agent)
        sender = request.requested_by_agent.slug if request.requested_by_agent_id else agent.slug
        turn_ids = []
        for i, prompt in enumerate(s for s in p.prompts if s.strip()):
            once = f"approval-{request.pk}-{i}"          # one turn per (request, prompt), however often it is retried
            turn, _ = harness.enqueue_turn(
                agent=agent, origin="api", idempotency_key=once,
                prompt=stamp_dispatched(prompt.strip(), sender=sender),
                origin_ref={"trigger": "approval_request", "approval_request": request.pk,
                            "task_title": request.title},
                initiator=who.for_user(user, via=f"approval:{request.key}", assurance=who.APPROVAL))
            turn_ids.append(str(turn.pk))
        return {"agent": agent.slug, "turns": turn_ids}
