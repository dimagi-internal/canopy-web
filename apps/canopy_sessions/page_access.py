"""Whose page may this caller see? One predicate, two callers.

**The bug this exists to fix.** Both page surfaces — `page_tools.py` (what a
page can DO) and `tools/page.py` (what a page SHOWS) — resolved the caller from
the MCP access token and then filtered `Session.created_by = that user`. That is
correct for a human asking about their own tabs, and it is the only case anyone
ever tested. In production it is never the case: an agent authenticates to
canopy's own MCP with its OWN PAT (`secret_refs: canopy-pat`), so the caller is
`ace@dimagi-ai.com` while the session was created by `jjackson@dimagi.com`.
The filter matched nothing, so the agent saw no page tools and an empty page
state — silently, with every test green, because every test had the caller and
the session owner be the same person.

So the embedded widget's two headline features could not work at all, and
nothing said so: an agent with no page tools simply answers without them.

**The rule.** A caller may see a session's page if they are one of the two
parties actually in the conversation:

  * the human who created it — reading their own screen; or
  * the AGENT that session is with, asking about the screen of the person it is
    talking to.

Anything else is a third party, including another agent in the same workspace.
Note this is deliberately NARROWER than workspace membership: a co-tenant can
already not read the chat (`access.py`), and the page is strictly more intimate
than the chat — it is a live picture of somebody's screen.

**Why an explicit `Agent.user` and not an email match.** `Agent.email` is blank
by default and unique on neither side, so `agent.email == user.email` authorizes
every emailless agent against every emailless user. That is the
`workspace_id IS NULL means allow` failure this repo has already paid for once
(six predicates, four PRs, one NOT NULL constraint). The FK is explicit, and
every query here compares concrete ids so a NULL can never match.
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
    return Q(created_by_id=user_id) | Q(agent__user_id=user_id)


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
