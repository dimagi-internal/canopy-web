"""Whose page may a USER see? Their own — and only their own.

A page is a live picture of someone's screen. There are three ways to ask
about one, each answering for exactly one party:

* **A person, about their own tabs** — this module: `created_by` is them.
* **The session driving a chat, about THAT chat's page** — its chat key
  (`chat_keys`, `apps/mcp/chat_scope.py`), which canopy issued when the chat's
  turn was claimed and which names one chat.
* **A confined caller's session, about its own conversation** — its caller
  token (`apps/mcp/turn_scope.caller_turn_ids`).

There used to be a fourth leg here: the AGENT's login (`Agent.user`) matched
every chat the agent was in. That was how an agent, calling canopy's MCP with
its own PAT, first got to see the page of the person it was talking to — and it
answered too broadly (every chat of that agent, not the one being driven) by
inferring a permission from an identity. The chat key replaced it, and the leg
was removed on 2026-09-26. `Agent.user` now means only "this login is the
agent", which is what `agents/access.py` reads it for.

An absent caller matches NOTHING — `Q(pk__in=[])`, never an empty `Q()`, which
is the identity and would widen the caller's filter to every session on the
deployment. That inversion is the single most dangerous thing this file could
get wrong.
"""

from __future__ import annotations

from django.db.models import Q

from .models import Session


def page_visible_q(user) -> Q:
    """Sessions whose page `user` may see, as a predicate.

    Returned as a `Q` rather than a queryset so both callers compose it with
    their own filters (active-only, has-actions, has-state) without this module
    having to know what either wants.

    An unauthenticated or None caller matches NOTHING — `Q(pk__in=[])` rather
    than an empty `Q()`, because an empty `Q` is the identity and would silently
    widen the caller's filter to every session on the deployment. That inversion
    is the single most dangerous thing this file could get wrong.
    """
    user_id = getattr(user, "pk", None)
    if not user_id:
        return Q(pk__in=[])
    return Q(created_by_id=user_id)


def sessions_with_page_for(user):
    """Active sessions whose page `user` may see, newest first.

    Newest-first because when a person has two tabs open, "the one I just
    opened" is the better guess at which they mean — and the caller is told
    which session each answer came from either way, so a wrong guess is visible
    rather than silent.
    """
    return (
        Session.objects.filter(page_visible_q(user), status=Session.ACTIVE)
        .select_related("agent")
        .order_by("-created_at")
    )
