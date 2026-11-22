"""/api/agents/{slug}/browser-sign-in — let the agent's own Google account sign in to
canopy-web at a browser, for testing (`apps/agents/browser_sign_in.py`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.route_gates import gate
from apps.events import audit
from apps.passkeys import stepup

from . import browser_sign_in
from .gates import agent_for_admin
from .models import Agent
from .schemas import BrowserSignInIn, BrowserSignInOut

router = Router(auth=session_auth, tags=["agents"])

_NOT_ITS_OWN = "an agent's own login cannot see or change its browser sign-in"


def _agent(request: HttpRequest, slug: str) -> Agent:
    """The agent, for its owner or an admin — never its own login, whatever it holds
    (an agent made its own `AgentAdmin` included)."""
    agent = agent_for_admin(request, slug)
    if agent.user_id is not None and agent.user_id == request.user.pk:
        raise HttpError(403, _NOT_ITS_OWN)
    return agent


def _audit_view(st: dict) -> dict:
    return {"enabled": st["enabled"], "expires_at": st.get("expires_at").isoformat() if st.get("expires_at") else None}


@router.get("/{slug}/browser-sign-in", response=BrowserSignInOut,
            summary="Whether the agent's own Google account may sign in at a browser")
@gate("agent-admin")
def get_browser_sign_in(request: HttpRequest, slug: str) -> BrowserSignInOut:
    """Off unless switched on; while on, who switched it on, when, and when it ends.
    The agent's owner or an admin; never the agent's own login."""
    return BrowserSignInOut(**browser_sign_in.state(_agent(request, slug)))


@router.put("/{slug}/browser-sign-in", response=BrowserSignInOut,
            summary="Switch on browser sign-in for testing, for 1 to 30 days")
# A person's act at a browser: + passkey step-up, so a token (the owner's included) is refused.
@gate("agent-admin", setting=True, step_up=True)
def switch_on_browser_sign_in(request: HttpRequest, slug: str, payload: BrowserSignInIn) -> BrowserSignInOut:
    """FOR TESTING: while on, the agent's own Google account may sign in to canopy-web
    at a browser (e.g. a live end-to-end test of the browser extension's side panel)
    instead of being refused. The session is the agent's login, with exactly its
    normal permissions, and is signed out when this ends or is switched off. `days`
    (1 to 30, default 7) sets the end; switching on again replaces it. The agent's
    owner or an admin, at a browser, with a fresh passkey step-up; 409
    `no_agent_login` when the agent has no login of its own."""
    agent = _agent(request, slug)
    stepup.require_recent_step_up(request)
    if agent.user_id is None:
        raise ProblemError(409, "Browser sign-in", type_=TYPE_CONFLICT,
                           detail="This agent has no canopy login of its own to sign in as.",
                           members={"error": "no_agent_login"})
    before = browser_sign_in.state(agent)
    browser_sign_in.switch_on(agent, request.user, days=payload.days, reason=payload.reason)
    after = browser_sign_in.state(agent)
    audit.note_change(scope=f"agent:{agent.slug}/browser-sign-in", before=_audit_view(before),
                      after=_audit_view(after))
    return BrowserSignInOut(**after)


@router.delete("/{slug}/browser-sign-in", response=BrowserSignInOut,
               summary="Switch off browser sign-in; its sessions end on their next request")
# Taking it away never waits on a passkey.
@gate("agent-admin", setting=True)
def switch_off_browser_sign_in(request: HttpRequest, slug: str) -> BrowserSignInOut:
    """Ends the grant now; a browser session made under it is signed out on its next
    request. The agent's owner or an admin, from anywhere; never its own login."""
    agent = _agent(request, slug)
    before = browser_sign_in.state(agent)
    browser_sign_in.switch_off(agent, request.user)
    after = browser_sign_in.state(agent)
    audit.note_change(scope=f"agent:{agent.slug}/browser-sign-in", before=_audit_view(before),
                      after=_audit_view(after))
    return BrowserSignInOut(**after)
