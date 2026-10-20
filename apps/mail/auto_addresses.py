"""Every agent gets `<slug>@<domain>`: a domain's automatic agent addresses.

With `MailDomain.agent_addresses` on, each agent in a workspace that may use the domain
(its own workspace, and every workspace it is shared with and the workspaces below those:
`domains.may_use`) holds a real agent route for its slug — the same route an admin would
add by hand, so threads, replies, `send --from` and the agent's address list need nothing
new. The routes are marked `auto` and `sync` keeps them true: it adds the missing ones and
deletes the ones whose agent left the domain's reach or whose switch went off.

It never takes an address: a slug another route already holds (a forward, another agent,
a role address like postmaster@) is skipped and reported, and a route an admin edits stops
being automatic. Agents may have other inboxes besides this one; this only adds one more.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q

from apps.agents.models import Agent
from apps.workspaces import services as wsvc

from . import domains
from .models import MailDomain, MailRoute

log = logging.getLogger(__name__)


def covered(domain: MailDomain) -> set[str]:
    """Slugs of every workspace that may use `domain` (the rule `domains.may_use` applies)."""
    shared = set(domain.shares.values_list("workspace_id", flat=True))
    return {domain.workspace_id} | shared | wsvc.descendant_slugs(shared)


def sync(domain: MailDomain) -> dict:
    """Make the domain's automatic routes match its agents. Returns what changed:
    `{"created": [address], "removed": [address], "skipped": {slug: reason}}`."""
    out: dict = {"created": [], "removed": [], "skipped": {}}
    want: dict[str, Agent] = {}
    if domain.agent_addresses:
        for a in Agent.objects.filter(workspace_id__in=covered(domain)).select_related("workspace"):
            want[a.slug.lower()] = a
    held = {r.local_part: r for r in domain.routes.select_related("agent")}
    for local, r in list(held.items()):
        if r.auto and _settle(r, want.get(local), out):
            del held[local]
    for local, a in want.items():
        _ensure(domain, a, held.get(local), out)
    if out["created"] or out["removed"]:
        log.info("mail: agent addresses on %s: +%s -%s", domain.name, out["created"], out["removed"])
    return out


def sync_agent(domain: MailDomain, agent: Agent) -> dict:
    """`sync` for one agent: what an agent's create or move can change, in a few queries."""
    out: dict = {"created": [], "removed": [], "skipped": {}}
    local = agent.slug.lower()
    r = domain.routes.filter(local_part=local).select_related("agent").first()
    a = agent if domain.agent_addresses and domains.may_use(domain, agent.workspace) else None
    if r is not None and r.auto and _settle(r, a, out):
        r = None
    if a is not None:
        _ensure(domain, a, r, out)
    return out


def _settle(r: MailRoute, a: Agent | None, out: dict) -> bool:
    """An automatic route whose agent is `a` (None: no agent may hold it now). Deletes it
    when it no longer belongs, follows the agent's move otherwise. True when deleted."""
    if a is None or r.kind != MailRoute.AGENT or r.agent_id != a.pk:
        out["removed"].append(r.address)
        r.delete()  # its threads stay (route → NULL), as for any removed address
        return True
    if r.workspace_id != a.workspace_id:  # the agent moved, still within the domain's reach
        r.workspace_id = a.workspace_id
        r.save(update_fields=["workspace"])
    return False


def _ensure(domain: MailDomain, a: Agent, r: MailRoute | None, out: dict) -> None:
    """Give `a` its address unless it has it (automatic or by hand) or another route holds it."""
    if r is not None:
        if not (r.auto or (r.kind == MailRoute.AGENT and r.agent_id == a.pk)):
            out["skipped"][a.slug] = f"{r.address} is already taken"
        return
    try:
        route = domains.save_route(domain, local_part=a.slug.lower(), kind=MailRoute.AGENT,
                                   agent_slug=a.slug, workspace=a.workspace)
    except domains.DomainError as exc:
        out["skipped"][a.slug] = str(exc)
        return
    MailRoute.objects.filter(pk=route.pk).update(auto=True)
    out["created"].append(route.address)


def sync_all() -> None:
    for d in MailDomain.objects.filter(agent_addresses=True):
        sync(d)


def skipped_here(domain: MailDomain, workspace) -> list[str]:
    """This workspace's agents the switch could not give an address (another route holds
    the slug). Only the viewer's own agents: never another tenant's."""
    if not domain.agent_addresses or not domains.may_use(domain, workspace):
        return []
    mine = set(domain.routes.filter(kind=MailRoute.AGENT, agent__workspace=workspace)
               .values_list("agent__slug", flat=True))
    return sorted(s for s in Agent.objects.filter(workspace=workspace).values_list("slug", flat=True)
                  if s not in mine)


# An agent created or moved, or a workspace re-parented, can change who is in a domain's
# reach (a deleted agent's routes go with it). Synced after the commit, and never allowed
# to fail the save that caused it.
def _after_commit(fn) -> None:
    def run():
        try:
            fn()
        except Exception:
            log.exception("mail: agent addresses sync failed")
    transaction.on_commit(run)


def on_agent_saved(sender, instance, created, update_fields=None, **kwargs) -> None:
    if not (created or update_fields is None or "workspace" in update_fields):
        return
    # The domains that may change: those with the switch on, and any still holding an
    # automatic address for this agent (the switch went off there, or the agent left).
    which = Q(agent_addresses=True) | Q(routes__agent_id=instance.pk, routes__auto=True)
    if not MailDomain.objects.filter(which).exists():
        return
    pk = instance.pk

    def run():
        agent = Agent.objects.select_related("workspace").filter(pk=pk).first()
        if agent is not None:
            for d in MailDomain.objects.filter(which).distinct():
                sync_agent(d, agent)
    _after_commit(run)


def on_workspace_saved(sender, instance, created, update_fields=None, **kwargs) -> None:
    if not created and (update_fields is None or "parent" in update_fields):
        if MailDomain.objects.filter(agent_addresses=True).exists():
            _after_commit(sync_all)
