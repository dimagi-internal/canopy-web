"""The mail app's part in moving an agent between workspaces (`apps/agents/move.py`).

Registered from `MailConfig.ready()`: the move is framework tier and may not
import this product app, so the app hands its tables in instead.

- **Threads and their messages** are the agent's mail history: they move.
- **Routes** (the agent's addresses) sit on a domain. One the destination may use
  — its own, or one shared with it or an ancestor (`domains.may_use`) — moves
  with the agent and keeps receiving. One it may not use cannot follow: the
  route is DISABLED where it is, so the address stops routing to the agent
  rather than opening new threads in a tenant the agent has left (the silent
  half-move this replaces). The dry run names each such address.
"""
from __future__ import annotations

from apps.agents.move import Plan

from . import domains
from .models import MailMessage, MailRoute, MailThread


def participant(p: Plan, apply: bool) -> None:
    agent, dest = p.agent, p.dest

    threads = MailThread.objects.filter(agent_slug=agent.slug).exclude(workspace_id=dest.slug)
    messages = MailMessage.objects.filter(thread__in=threads)
    if apply:
        # Messages first: their filter goes through the threads still in the source.
        n_messages = messages.update(workspace=dest)
        n_threads = threads.update(workspace=dest)
    else:
        n_messages, n_threads = messages.count(), threads.count()
    p.moves("mail.MailThread", n_threads, "re-homed")
    p.moves("mail.MailMessage", n_messages, "re-homed with its thread")

    moved = 0
    for route in MailRoute.objects.filter(agent=agent).exclude(workspace_id=dest.slug).select_related("domain"):
        if _may_hold(route, dest):
            moved += 1
            if apply:
                route.workspace = dest
                route.save(update_fields=["workspace"])
            continue
        p.breaks("mail_route",
                 f"{route.address} is on {route.domain.name}, which '{dest.slug}' cannot use "
                 f"(owned by '{route.domain.workspace_id}', not shared with it): the address "
                 "is disabled and stops routing to the agent. Share the domain with the "
                 "destination first to keep it")
        if apply and route.enabled:
            route.enabled = False
            route.save(update_fields=["enabled"])
    p.moves("mail.MailRoute", moved, "re-homed (domain usable by the destination)")


def _may_hold(route: MailRoute, dest) -> bool:
    """May `dest` hold this address — the test `domains.save_route` applies."""
    if route.domain.workspace_id == dest.slug:
        return True
    if not domains.may_use(route.domain, dest):
        return False
    from django.conf import settings

    return route.local_part not in domains._OWNER_ONLY and route.local_part != settings.MAIL_DMARC_LOCAL_PART
