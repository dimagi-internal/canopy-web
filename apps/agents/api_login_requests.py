"""`/api/me/agent-login-requests/` — requests to make YOUR canopy account an agent's login.

An agent's login is the account its token signs in as, so an agent linked to your
account acts as you. An admin may ask (`PUT /api/agents/{slug}/canopy-user`); only you
can say yes, in your own browser behind a passkey step-up (`agent_login.decide_link`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.passkeys import stepup

from . import agent_login
from .api import login_request_out
from .models import AgentLoginRequest
from .schemas import AgentLoginRequestOut
from .services import AgentUserLinkError

router = Router(tags=["agents"])


@router.get("/", response=list[AgentLoginRequestOut],
            summary="Requests to make your canopy account an agent's own login")
@gate("self")
def list_my_agent_login_requests(request: HttpRequest) -> list[AgentLoginRequestOut]:
    """Open requests, from an agent's admin, to make your existing canopy account
    that agent's own login — after which the agent's token signs in as you. Only
    you can accept one, in the canopy web app."""
    return [AgentLoginRequestOut(**login_request_out(r)) for r in agent_login.pending_for(request.user)]


def _mine(request: HttpRequest, request_id: int) -> AgentLoginRequest:
    row = (AgentLoginRequest.objects.select_related("agent", "user", "requested_by")
           .filter(pk=request_id, user=request.user).first())
    if row is None:
        raise HttpError(404, "request not found")
    return row


@router.post("/{request_id}/accept", response=AgentLoginRequestOut,
             summary="Make your canopy account this agent's own login (browser, passkey)")
@gate("self", setting=True, step_up=True)
def accept_agent_login_request(request: HttpRequest, request_id: int) -> AgentLoginRequestOut:
    """Link your account to the agent that asked: from then on the agent's own
    token signs in as you. Only in your own browser, after a passkey step-up; a
    token is refused (403), so no agent or script can say yes for you. 422 when
    the request is closed, expired, or whoever asked is no longer the agent's
    admin."""
    if not stepup.is_browser(request):
        raise HttpError(403, "only you can accept this, in the canopy web app — not with a token")
    stepup.require_recent_step_up(request)
    row = _mine(request, request_id)
    try:
        agent_login.decide_link(row, request.user, accept=True)
    except AgentUserLinkError as exc:
        raise HttpError(422, str(exc)) from exc
    return AgentLoginRequestOut(**login_request_out(row))


@router.post("/{request_id}/decline", response=AgentLoginRequestOut,
             summary="Decline making your canopy account an agent's login")
@gate("self", setting=True)
def decline_agent_login_request(request: HttpRequest, request_id: int) -> AgentLoginRequestOut:
    """Say no: the agent is not linked to your account. 422 when it is no longer open."""
    row = _mine(request, request_id)
    try:
        agent_login.decide_link(row, request.user, accept=False)
    except AgentUserLinkError as exc:
        raise HttpError(422, str(exc)) from exc
    return AgentLoginRequestOut(**login_request_out(row))
