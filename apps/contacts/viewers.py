"""Letting an agent see your memory page (/people/me), read-only.

The person's own act, on their page: they name an agent, and that agent's OWN
canopy login (`Agent.user`, its PAT) may then read what the page shows them —
what is held, who may read it, what happened, how agents searched it — and
nothing else. It changes nothing: every write stays the person's
(`people_api._the_person_themself`). A caller token, a session acting for
someone, or another agent's login is "not found", so an id confirms nothing.

Every look is in the person's audit (`page.viewed`, at most one per
`LOOK_WINDOW`, since a page load is several reads) and among the reads on their
page (`PersonAccess`). Revoking ends it at once; nothing revives it but a new
grant.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from . import hcp, people
from .models import MemoryViewer, Person, PersonAccess

#: One `page.viewed` per agent per this long: a page load reads five things.
LOOK_WINDOW = dt.timedelta(minutes=10)


def live(person: Person):
    return (MemoryViewer.objects.filter(person=person, revoked_at__isnull=True)
            .select_related("agent").order_by("granted_at"))


def grant(person: Person, agent, *, user) -> MemoryViewer:
    """Let `agent` see `person`'s page. Granting again is a no-op."""
    with transaction.atomic():
        current = live(person).filter(agent=agent).first()
        if current is not None:
            return current
        viewer = MemoryViewer.objects.create(person=person, agent=agent, granted_by=user)
        hcp.audit(person, "viewer.granted", actor=hcp.user_actor(user),
                  purpose=f"let {agent.name or agent.slug} see this page, read-only")
    return viewer


def revoke(person: Person, agent, *, user) -> bool:
    """Stop `agent` seeing `person`'s page. False when it could not."""
    with transaction.atomic():
        n = live(person).filter(agent=agent).update(revoked_at=timezone.now())
        if n:
            hcp.audit(person, "viewer.revoked", actor=hcp.user_actor(user),
                      purpose=f"stop {agent.name or agent.slug} seeing this page")
    return bool(n)


def viewing_agent(request):
    """The agent whose own login made this request — never a caller token or
    an HCP OAuth client — else None."""
    if (getattr(request, "auth_method", "") == "caller_token"
            or getattr(request, "hcp_access", None) is not None):
        return None
    return people.agent_of_login(request.user)


def shared_with(agent) -> list[MemoryViewer]:
    return list(MemoryViewer.objects.filter(agent=agent, revoked_at__isnull=True)
                .select_related("person", "person__user").order_by("granted_at"))


def page_of(request, person_id: int) -> Person | None:
    """`person_id`'s Person, if the agent behind this request may see their
    page; else None (the caller answers "not found"). Notes the look."""
    agent = viewing_agent(request)
    if agent is None:
        return None
    viewer = (MemoryViewer.objects.filter(person_id=person_id, agent=agent, revoked_at__isnull=True)
              .select_related("person").first())
    if viewer is None:
        return None
    now = timezone.now()
    if viewer.last_viewed_at is None or now - viewer.last_viewed_at >= LOOK_WINDOW:
        with transaction.atomic():
            # Claim the window in one UPDATE, so two reads at once write one event.
            claimed = MemoryViewer.objects.filter(pk=viewer.pk, last_viewed_at=viewer.last_viewed_at) \
                .update(last_viewed_at=now)
            if claimed:
                hcp.audit(viewer.person, "page.viewed", actor=hcp.agent_actor(agent),
                          purpose="see your memory page, as you let it")
                PersonAccess.objects.create(person=viewer.person, reader_agent=agent,
                                            reader_user=request.user, via=PersonAccess.VIA_API)
    return viewer.person
