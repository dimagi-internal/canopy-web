"""Creating an agent's OWN canopy login — a deliberate door into a workspace.

An agent calls canopy-web as itself (`Agent.user`, signed in with its `canopy-pat`), and a
login must be a workspace member to act there. Until 2026-10-15 such logins were made only
by `manage.py create_token --create-user` on the server, so an agent created in the browser
never got one and its turns ran on a person's token.

This is the one place that makes such a member, and it is listed among the doors in
`docs/architecture/roles.md` ("How you get into a workspace") and in
`tests/test_no_implicit_enrolment.py`'s allow-list. It stays narrow on purpose:
  - the caller must be able to INVITE (`perms.MEMBERS_MANAGE`) — a new member is what an
    invite makes, so no one gets a way in they did not already have;
  - the login is brand new (an existing account is never adopted — that would be minting a
    token for a person), has no password, is an editor, and is linked to exactly one agent.
"""
from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import Agent, AgentLoginRequest
from .services import AgentUserLinkError, link_canopy_user


class AgentLoginRefused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def create_agent_login(agent, actor, address: str):
    """The new user, linked to `agent`. Raises AgentLoginRefused with the HTTP status."""

    if not perms.can(actor, agent.workspace, perms.MEMBERS_MANAGE):
        raise AgentLoginRefused(
            f"{agent.slug} has no canopy login yet, and creating one adds a member to the "
            "workspace — which takes a workspace admin or owner (the people who can invite)", 403)
    if get_user_model().objects.filter(email__iexact=address).exists():
        raise AgentLoginRefused(
            f"{address} is already a canopy user. This only CREATES an agent's login; to use "
            f"that account, link it on {agent.slug}'s settings (canopy user) — the person "
            "holding it accepts", 409)
    with transaction.atomic():
        user = get_user_model().objects.create_user(username=address, email=address)
        user.set_unusable_password()
        user.save(update_fields=["password"])
        wsvc.ensure_member(agent.workspace, user)  # its default role: editor
        link_canopy_user(agent, user.pk)
    return user


def follow_agent(agent, actor, dest) -> bool:
    """Make the agent's EXISTING login a member of `dest`, the workspace the agent is
    moving to (`apps/agents/move.py`) — the same membership `create_agent_login` gives
    it at home. Same bar: the actor must be able to invite in `dest`. Returns whether
    the login is (now) a member; False when there is no login or the actor may not."""
    login = agent.user
    if login is None:
        return False
    if wsvc.is_member(login, dest.slug):
        return True
    if not perms.can(actor, dest, perms.MEMBERS_MANAGE):
        return False
    wsvc.ensure_member(dest, login)  # its default role: editor
    return True


# ---- linking an EXISTING account: its person says yes --------------------------------

#: How long a request to link someone's account waits for them.
LINK_REQUEST_TTL = dt.timedelta(days=7)


def account_at_email(agent):
    """The active canopy account at the agent's own email (`Agent.email`), or None."""
    address = (agent.email or "").strip()
    if "@" not in address:
        return None
    return get_user_model().objects.filter(email__iexact=address, is_active=True).order_by("pk").first()


def link_by_email(agent, actor):
    """Link the agent to its canopy login: ALWAYS the account at its own email
    (`Agent.email`), never any other. Returns the pending `AgentLoginRequest` when the
    person holding that account must accept, else None (linked). Raises
    AgentUserLinkError, or AgentLoginRefused when creating the account is refused.

    - No account at that email: canopy creates it as the agent's login (never signed in
      interactively, `create_agent_login`) and links it.
    - An existing account nobody has ever signed in to interactively: linked — there
      is no person behind it to accept.
    - The actor's OWN account (the route stepped them up): linked.
    - Anyone else's account that HAS signed in: canopy can't tell an agent mailbox
      from a person's, so the person holding it must accept, in their browser behind
      a passkey (`decide_link`)."""
    address = (agent.email or "").strip().lower()
    if "@" not in address:
        raise AgentUserLinkError(f"{agent.slug} has no email: its canopy login is the account at "
                                 "its own email, so set the agent's email first")
    if agent.user_id is not None and (agent.user.email or "").lower() == address:
        withdraw_pending(agent)
        return None
    target = account_at_email(agent)
    if target is None:
        withdraw_pending(agent)
        if agent.user_id is not None:
            link_canopy_user(agent, None)
        create_agent_login(agent, actor, address)
        return None
    _check_linkable(agent, target)
    if target.last_login is None or target.pk == getattr(actor, "pk", None):
        # Nobody has ever signed in to it interactively (tokens never set `last_login`):
        # there is no person behind it to accept, so it is the agent's mailbox account
        # (the same rule as migration agents/0096). Or it is the actor's own (stepped up).
        withdraw_pending(agent)
        link_canopy_user(agent, target.pk)
        return None
    with transaction.atomic():
        withdraw_pending(agent)
        return AgentLoginRequest.objects.create(
            agent=agent, user=target, requested_by=actor,
            expires_at=timezone.now() + LINK_REQUEST_TTL)


def unlink(agent) -> None:
    withdraw_pending(agent)
    link_canopy_user(agent, None)


def rederive_after_email_change(agent, actor=None) -> None:
    """`Agent.email` changed: a login at the old address is no longer the agent's, so it
    is unlinked; then, when someone is acting, the link is derived again from the new
    email (`link_by_email`) — best-effort: a refusal leaves the agent unlinked."""
    login = agent.user if agent.user_id else None
    if login is not None and (login.email or "").lower() == (agent.email or "").strip().lower():
        return
    if login is not None:
        unlink(agent)
    else:
        withdraw_pending(agent)
    if actor is None or "@" not in (agent.email or ""):
        return
    try:
        link_by_email(agent, actor)
    except (AgentUserLinkError, AgentLoginRefused):
        pass


def _check_linkable(agent, target) -> None:
    """The same refusals `link_canopy_user` makes, asked BEFORE a request goes to a person."""
    other = Agent.objects.filter(user=target).exclude(pk=agent.pk).first()
    if other is not None:
        raise AgentUserLinkError(f"{target.email} is already the canopy user of '{other.slug}' — "
                                 "one user is one agent instance; unlink it there first")
    if not wsvc.is_member(target, agent.workspace_id):
        raise AgentUserLinkError(f"{target.email} is not a member of this agent's workspace")


def withdraw_pending(agent) -> None:
    AgentLoginRequest.objects.filter(agent=agent, status=AgentLoginRequest.PENDING).update(
        status=AgentLoginRequest.WITHDRAWN, decided_at=timezone.now())


def pending_request(agent):
    """The agent's open, unexpired request, or None."""
    return (AgentLoginRequest.objects.select_related("user", "requested_by")
            .filter(agent=agent, status=AgentLoginRequest.PENDING, expires_at__gt=timezone.now())
            .first())


def pending_for(user):
    """Requests waiting on `user` to make their account an agent's."""
    return list(AgentLoginRequest.objects.select_related("agent", "requested_by")
                .filter(user=user, status=AgentLoginRequest.PENDING, expires_at__gt=timezone.now()))


def decide_link(request_row, user, *, accept: bool):
    """`user` accepts or declines a request addressed to them. Accepting links their
    account to the agent — re-checked now: the asker must still be an admin of it, and
    the account still linkable. Raises AgentUserLinkError."""
    if request_row.user_id != user.pk or request_row.status != AgentLoginRequest.PENDING \
            or request_row.expires_at <= timezone.now():
        raise AgentUserLinkError("this request is not open, or not yours")
    agent = request_row.agent
    with transaction.atomic():
        if accept:
            asker = request_row.requested_by
            if asker is None or not agent.is_admin(asker):
                raise AgentUserLinkError(f"whoever asked is no longer an admin of {agent.slug}")
            _check_linkable(agent, user)
            link_canopy_user(agent, user.pk)
        request_row.status = AgentLoginRequest.ACCEPTED if accept else AgentLoginRequest.DECLINED
        request_row.decided_at = timezone.now()
        request_row.save(update_fields=["status", "decided_at"])
    return agent
