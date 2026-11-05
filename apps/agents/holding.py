"""May a box run as an agent — the one predicate behind every place an agent's identity
leaves canopy. A leaf (models, `sensitive_data`, `harness.runner_requirements`), so the
runner protocol in `apps/harness` can ask it at module top."""
from __future__ import annotations

from apps.harness.runner_requirements import is_sensitive_data_box

from . import sensitive_data


def runner_may_hold_agent(runner, agent, *, standings=None) -> bool:
    """May this box run AS this agent — hold its prompt, its caller's token, its
    secrets and its owner's GitHub identity?

    Only when its owner is one of the agent's admins (its owner, a
    workspace owner, or an `AgentAdmin`). A runner speaks with its owner's
    authority, so this is "is the owner trusted with the whole agent?", asked of
    the box. The one predicate behind every place an agent's identity leaves
    canopy: claiming an agent turn, the per-turn GitHub token, credential
    resolve, and the routing writes that point work at a box.

    The agent's OWN canopy login (`Agent.user`) is NOT a holder: pairing a box
    as an agent has been refused since #1049, so a box owned by an agent login
    is a pre-#1049 leftover, and the agent's own login is no admin of it.

    Fails closed on a runner with no owner.

    A sensitive-data box (`is_sensitive_data_box`) is dedicated: it holds only an
    agent with a connected site that handles sensitive data, whoever owns it — so no
    other agent's full profile, secrets or GitHub identity ever shares a box with a
    sensitive conversation (spec
    2026-10-22-sensitive-data-agents §1, "A dedicated box").

    `standings` (`access.Standings` holding the runner's owner and the agent)
    answers the admin question from one read, for a caller asking many pairs."""
    if getattr(runner, "owner_id", None) is None:
        return False
    if is_sensitive_data_box(runner) and not sensitive_data.sensitive_capabilities(agent.interface):
        return False
    if standings is not None:
        return standings.is_admin(runner.owner, agent)
    return agent.is_admin(runner.owner)
