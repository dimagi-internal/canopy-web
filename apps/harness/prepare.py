"""Get a page turn's runner ready before the person has finished typing.

A cloud runner spends its first seconds on a chat turn making the conversation's
working copy: pulling the agent's clone, installing it as its plugin and cutting the
session's worktree from it (`cloud_runner._turn_cwd`). None of that depends on the
turn — no prompt, no credential, no person — only on which agent and which
conversation. So when a conversation is opened on a site's page whose agent asked
for lean page turns (`published_profiles.page_lean_of`), canopy-web rings the cloud
runners that could take its turns with a `prepare` frame naming the agent and the
conversation, and a runner that holds the agent does that work now
(`cloud_runner._prepare_session`). The turn itself is claimed, resolved and run
exactly as before: preparing grants nothing and starts nothing.

Rung when the conversation is created and when its page first declares its state
(the panel attaching), at most once per `PREPARE_EVERY` per conversation.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: A conversation is prepared at most this often (seconds).
PREPARE_EVERY = 120


def candidate_runners(agent) -> list:
    """The cloud runners that could run `agent`'s turns: its own runner list and its
    workspace's runner order, each one a box whose owner may hold the agent."""
    from apps.agents.holding import runner_may_hold_agent  # noqa: PLC0415 — agents imports harness

    from .models import Runner, RunnerAssignment, WorkspaceRunnerOrder  # noqa: PLC0415 — models load with a call

    ids = set(RunnerAssignment.objects.filter(agent=agent, enabled=True).values_list("runner_id", flat=True))
    ids |= set(WorkspaceRunnerOrder.objects.filter(workspace_id=agent.workspace_id, enabled=True)
               .values_list("runner_id", flat=True))
    runners = (Runner.objects.filter(pk__in=ids, kind=Runner.CLOUD)
               .exclude(status=Runner.RETIRED))
    return [r for r in runners if runner_may_hold_agent(r, agent)]


def ring(session) -> int:
    """Ring the runners that could take `session`'s turns to prepare it. Returns how many
    were rung (0 when the conversation is not a lean page conversation, or was rung
    lately). Never raises: preparing is an optimisation, never a reason a write fails."""
    try:
        return _ring(session)
    except Exception:  # noqa: BLE001
        logger.exception("could not ring runners to prepare session %s", getattr(session, "pk", None))
        return 0


def _ring(session) -> int:
    from apps.agents import published_profiles  # noqa: PLC0415 — the interface reads models, loaded with a call
    from apps.realtime import groups  # noqa: PLC0415 — realtime imports harness

    agent = getattr(session, "agent", None)
    site = published_profiles.session_site(session)
    if agent is None or not site or site not in published_profiles.page_sites(agent):
        return 0
    if not published_profiles.page_lean_of(agent, site):
        return 0
    if not cache.add(f"harness.prepare:{session.pk}", 1, PREPARE_EVERY):
        return 0
    runners = candidate_runners(agent)
    for runner in runners:
        groups.publish(groups.runner_group(runner.pk), {
            "type": "runner.prepare", "agent": agent.slug, "session_id": str(session.pk)})
    return len(runners)
