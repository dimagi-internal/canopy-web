"""/api/agents/{slug}/github — the owner's (or an admin's) GitHub identity, delegated to the agent.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.passkeys import stepup

from .credentials import (
    delegations,
)
from .credentials.zk import ZkError
from .gates import (
    agent_for_read,
)
from .route_refusals import zk_problem
from .schemas import (
    AgentGitHubIn,
    AgentGitHubOut,
)

router = Router(auth=session_auth, tags=["agents"])

# ---- GitHub: the owner's (or an admin's) identity, delegated to this agent -----
# The owner or an admin pastes a fine-grained token here; it is sealed into the agent's
# vault as a credential delegated by them, which only the machines that run the agent can
# open (apps/agents/credentials/accounts.py, delegations.py).

@router.get("/{slug}/github", response=AgentGitHubOut,
            summary="How this agent acts on GitHub (masked — never the token)")
@gate("member")
def get_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Whose GitHub identity this agent's pull requests use, when that token
    expires, and whether it can open a pull request on the agent's repo."""
    agent = agent_for_read(request, slug)
    return _github_out(request, agent)


def _github_out(request: HttpRequest, agent) -> AgentGitHubOut:
    return AgentGitHubOut(**delegations.status(agent, machines=True, viewer=request.user),
                          can_lend=agent.is_admin(request.user))


@router.put("/{slug}/github", response=AgentGitHubOut,
            summary="Delegate your GitHub identity to this agent (owner or admin, write-only)")
@gate("agent-admin", setting=True, step_up=True)  # an owner or admin lends their OWN identity (delegations.set_github)
def set_agent_github(request: HttpRequest, slug: str, payload: AgentGitHubIn) -> AgentGitHubOut:
    """Give this agent the caller's fine-grained GitHub token, as a credential delegated
    by them. It replaces the one delegated now.

    Checked first: GitHub must accept it, and it must be able to push to and open a pull
    request on the agent's own repo; a token that fails is refused with the reason. Then
    it is sealed to the machines that run the agent and nothing else: no person can read
    it through canopy, the caller included, and canopy-web keeps no copy it can open.
    409 `no_machine_can_open` when no machine that runs the agent has an approved device
    key yet. A browser needs a fresh passkey step-up (403 `step_up_required`); a token may
    call it."""
    agent = agent_for_read(request, slug)
    if not agent.is_admin(request.user):   # before the step-up, as delegations.set_github says it
        raise HttpError(422, f"only {agent.slug}'s owner or an admin can delegate their GitHub identity to it")
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        delegations.set_github(agent, request.user, payload.token)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    except ZkError as exc:
        raise zk_problem(exc) from exc
    return _github_out(request, agent)


@router.post("/{slug}/github/check", response=AgentGitHubOut,
             summary="Re-check this agent's GitHub token against GitHub")
@gate(
    "member",
    kind="action",
    viewer_write=(
        "re-probes the stored token against GitHub and records the result; grants and changes nothing"
    ),
)
def check_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """A token held the old way is re-probed now. One delegated into the vault cannot be:
    canopy-web cannot open it, so the machines that run the agent probe it and report
    (`checked_by`), and this returns what they last found."""
    agent = agent_for_read(request, slug)
    delegations.check_github(agent)
    return _github_out(request, agent)


@router.delete("/{slug}/github", response=AgentGitHubOut,
               summary="Withdraw your GitHub identity from this agent")
# removes only the caller's own delegation
@gate("member", "self", setting=True, viewer_write="withdraws only the caller's OWN GitHub delegation")
def delete_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Removes the CALLER's own delegation — nobody can withdraw someone
    else's. A machine that opened it may have kept a copy: revoking the token at
    GitHub (github.com/settings/personal-access-tokens) is the only true recall."""
    agent = agent_for_read(request, slug)
    delegations.clear_github(agent, request.user)
    return _github_out(request, agent)


