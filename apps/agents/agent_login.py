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

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .services import link_canopy_user


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
            f"that account, link it on {agent.slug}'s settings (canopy user) first", 409)
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
