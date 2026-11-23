"""Browser sign-in: whether an agent's own Google account may open a canopy-web
session in a browser.

An agent's login usually reaches canopy with tokens, and by default its Google sign-in
is refused (`apps/common/auth_adapter.py`, reason `agent_login`). Some work needs a
browser session as the agent (the Dimagi Canopy extension's side panel, `/ext/panel/`,
and a live end-to-end test of it), so an agent's admins may allow it, per agent:

* **Who switches it on.** An admin of the agent (`Agent.is_admin`: its owner, a
  workspace owner, an `AgentAdmin`) — an ordinary agent setting
  (`PUT /api/agents/{slug}/browser-sign-in`, audited). The agent's own login never
  can, whatever it holds. It lasts until switched off, or for `days` (1 to
  `MAX_DAYS`) when an end is wanted. Switching off is `DELETE`.
* **What it allows.** While a grant is open (and, if it has an end, unexpired), the
  agent's Google account signs in instead of being refused (`grant_for_login`, asked by
  the adapter through `register_agent_browser_sign_in`). The session is the agent's
  login, with exactly the permissions its tokens already have: nothing here widens a
  gate, and every step-up-gated act stays refused to it
  (`passkeys.stepup.require_a_person`).
* **How it ends.** The session is marked (`SESSION_KEY`) and its cookie expires no
  later than the grant's end, if it has one (`mark_session`); `SessionMiddleware`
  signs it out on the first request after the grant expires or is switched off.

Docs: `docs/architecture/access.md` § Browser sign-in for an agent.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import logout
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.common import auth_adapter

from .models import Agent, AgentBrowserSignIn

#: The most days an end may be set at, when one is set. None (the default) is "until
#: switched off".
MAX_DAYS = 30
#: Marks a browser session made under a grant: `{"grant": id, "user": pk}`.
SESSION_KEY = "agents.browser_sign_in"


def _open(agent_id: int):
    return AgentBrowserSignIn.objects.filter(agent_id=agent_id, revoked_at__isnull=True)


def _in_force():
    return Q(revoked_at__isnull=True) & (Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))


def active(agent: Agent) -> AgentBrowserSignIn | None:
    """The agent's grant in force now, if any."""
    return (AgentBrowserSignIn.objects.filter(_in_force(), agent_id=agent.pk)
            .select_related("granted_by").first())


def grant_for_login(user_id: int) -> AgentBrowserSignIn | None:
    """The grant in force for the agent whose own login is `user_id`, if any."""
    return (AgentBrowserSignIn.objects.filter(_in_force(), agent__user_id=user_id)
            .select_related("agent").first())


def switch_on(agent: Agent, by, *, days: int | None = None, reason: str = "") -> AgentBrowserSignIn:
    """Open a grant, until switched off or for `days`, replacing any open one."""
    if days is not None and not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be 1..{MAX_DAYS} or None")
    now = timezone.now()
    with transaction.atomic():
        _open(agent.pk).update(revoked_at=now, revoked_by=by)
        return AgentBrowserSignIn.objects.create(
            agent=agent, granted_by=by, granted_at=now,
            expires_at=None if days is None else now + timedelta(days=days), reason=reason)


def switch_off(agent: Agent, by) -> int:
    """End every open grant now; its sessions are signed out on their next request."""
    return _open(agent.pk).update(revoked_at=timezone.now(), revoked_by=by)


def state(agent: Agent) -> dict:
    grant = active(agent)
    login = agent.user
    out = {"enabled": grant is not None, "login_email": getattr(login, "email", "") or "",
           "max_days": MAX_DAYS}
    if grant is not None:
        out.update(granted_by_email=getattr(grant.granted_by, "email", "") or "",
                   granted_at=grant.granted_at, expires_at=grant.expires_at, reason=grant.reason)
    return out


# ---- the session a grant opens ----


def mark_session(sender, request, user, **kwargs) -> None:
    """`user_logged_in`: a sign-in the adapter let through under a grant is marked, and
    its cookie ends no later than the grant's end, if it has one (nor later than an
    ordinary session). Any other sign-in as an agent login while a grant is open is held
    to it too, so no browser session as the agent outlives the grant however it was made."""
    if request is None or getattr(request, "session", None) is None or getattr(user, "pk", None) is None:
        return
    grant = getattr(request, auth_adapter.AGENT_BROWSER_SIGN_IN_ATTR, None) or grant_for_login(user.pk)
    if grant is None or grant.agent.user_id != user.pk:
        return
    request.session[SESSION_KEY] = {"grant": grant.pk, "user": user.pk}
    ordinary = timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE)
    request.session.set_expiry(ordinary if grant.expires_at is None else min(grant.expires_at, ordinary))


def session_in_force(request) -> bool:
    """A marked session's grant is still in force, for the same login."""
    mark = request.session.get(SESSION_KEY)
    user = getattr(request, "user", None)
    if not isinstance(mark, dict) or user is None or not user.is_authenticated or user.pk != mark.get("user"):
        return False
    return AgentBrowserSignIn.objects.filter(_in_force(), pk=mark.get("grant"), agent__user_id=user.pk).exists()


class SessionMiddleware:
    """Signs out a browser session made under a grant once the grant has expired or
    been switched off. After `AuthenticationMiddleware`; an unmarked session costs
    nothing but a dict lookup."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, "session", None)
        if session is not None and session.get(SESSION_KEY) is not None and not session_in_force(request):
            logout(request)
        return self.get_response(request)
