"""An agent login's TEST page permission: the one exception to "an act-as-you permission
on a host site is confirmed with a passkey".

A host grant (`tokens.HostGrant`, `apps/host_grants/host_grants.py`) is usable only once
its person confirmed it with their passkey. An agent's own canopy login (`Agent.user`) is
not a person: it never registers a passkey or steps up (`passkeys.stepup.require_a_person`).
So a headless test signed in to a host site AS the agent can never get past the panel's
"Confirm with passkey". Per agent and off by default:

* **Who turns it on.** An admin of the agent, from a browser, behind a fresh passkey
  step-up (`PUT /api/agents/{slug}/test-page-permission`, audited as a setting). A token
  cannot turn it on. Anyone who may turn it on may turn it off, from anywhere.
* **What it allows.** While it is on, that login may confirm its OWN waiting grant without
  a passkey (`POST /api/me/host-grants/{id}/confirm-as-agent`), with its own canopy
  credential: a PAT or OAuth token, or its own browser session. Never the panel's token,
  which the host site holds (`may_confirm`). The grant must be the login's own (user =
  the login, no contact), unexpired, still unconfirmed, and for the login's own agent or
  for no agent in particular. It lasts the usual window (`host_grants.confirm`).
* **What it never allows.** The login still registers no passkey and steps up nowhere;
  it confirms no person's grant (the grant is its own by construction) and acts as no
  person: the permission is to act as the agent's own login on that page.
* **Turning it off** stops every unexpired host grant of that login (`host_grants.revoke`),
  so nothing confirmed with the switch outlives it. A login cannot confirm a grant any
  other way, so every confirmed grant it holds came through the switch.

Docs: `docs/architecture/access-confinement-and-act-as.md` § Acting as the person on a
host site.
"""
from __future__ import annotations

from django.db import transaction

from apps.tokens import kinds

from ..models import Agent
from .login_test_key import NotAPerson, agent_of_login, is_person

#: The flag is per agent; this is what a person is told when they meet it.
LABEL = "test page permissions"

#: The credentials that are the login itself. Not a delegated token (the host site holds
#: the panel's), a caller or act-as token (a turn speaking for a person), a contact's, or
#: a runner's.
OWN_CREDENTIALS = frozenset({kinds.SESSION, kinds.PAT, kinds.OAUTH})


def state(agent: Agent) -> dict:
    """What the agent's Settings page shows: the switch, and the login's live grants."""
    from apps.host_grants import host_grants  # noqa: PLC0415 — host_grants imports agents

    live = len(host_grants.live_for(user=agent.user)) if agent.user_id else 0
    return {
        "enabled": agent.test_page_permission,
        "login_email": agent.user.email if agent.user_id else "",
        "live_grants": live,
    }


def enable(agent: Agent) -> None:
    if agent.user_id is None:
        raise NotAPerson(409, "no_agent_login",
                         f"{agent.slug} has no canopy login of its own to confirm a page permission with")
    agent.test_page_permission = True
    agent.save(update_fields=["test_page_permission", "updated_at"])


def disable(agent: Agent) -> int:
    """Turn it off and stop every unexpired host grant of the agent's login, confirmed or
    waiting. Returns how many grant rows it stopped."""
    from apps.host_grants import host_grants  # noqa: PLC0415 — host_grants imports agents
    from apps.tokens.models import HostGrant  # noqa: PLC0415 — beside the host_grants import

    n = 0
    with transaction.atomic():
        agent.test_page_permission = False
        agent.save(update_fields=["test_page_permission", "updated_at"])
        if agent.user_id is not None:
            rows = host_grants.unexpired(HostGrant.objects.filter(user_id=agent.user_id))
            for grant in rows.order_by("pk"):
                n += host_grants.revoke(grant)
    return n


def may_confirm(request, grant) -> None:
    """Raise `NotAPerson` unless `request` is an agent's own login, with its own
    credential, whose agent has the switch on, confirming its own grant."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        raise NotAPerson(401, "not_signed_in", "sign in as the agent's own canopy login")
    if is_person(user):
        raise NotAPerson(403, "a_person",
                         "a person confirms an act-as-you permission with their passkey, not this route")
    agent = agent_of_login(user)
    if agent is None:
        raise NotAPerson(403, "not_an_agent_login", "only an agent's own canopy login may use this route")
    if not agent.test_page_permission:
        raise NotAPerson(403, "test_page_permission_off",
                         f"{agent.slug}'s test page permissions are off; an admin of the agent can turn "
                         "them on (agent Settings → Security → Test page permissions)")
    kind = kinds.of_request(request)
    if kind is None or kind.name not in OWN_CREDENTIALS or getattr(request, "host_grant_id", None):
        raise NotAPerson(403, "not_own_credential",
                         "confirm with the agent's own canopy login (its token or its own browser "
                         "session), never a site's panel token")
    if grant.user_id != user.pk or grant.contact_id is not None:
        raise NotAPerson(404, "not_found", "no such permission")
    if grant.agent_id is not None and grant.agent_id != agent.pk:
        raise NotAPerson(403, "another_agents_page",
                         "this page's permission is for another agent; only the login's own agent's "
                         "page is confirmed this way")
    if grant.confirmed_at is not None:
        raise NotAPerson(409, "already_confirmed", "this permission is already confirmed")
