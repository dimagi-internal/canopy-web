"""Memory collections — the person's own named reaches (HCP 4.1.5.1).

A first-party agent's grant reaches its own organisation by default
(`hcp.agent_sources`), derived and never chosen in a prompt. A collection is how
the person, on /people/me ("More settings"), lets an agent reach something else —
their personal memory, or another organisation they belong to:

* A collection is built from CONTEXTS: `tree:<root>` for an organisation the
  person belongs to (or has entries in), and `personal`. A site is not a context.
* A grant using one names `collection:<id>` plus the sources it resolved to
  (Rule 3: the object says what it reaches) — `PersonGrant.collection`.
* Editing a collection re-resolves every live grant using it. Adding a source is
  a WIDENING: refused unless the person confirms, naming every affected grant
  (`NeedsConfirm` carries them, so the page can say "this will also let Hal and
  Eva use …"); each affected grant then gets a `grant.issued`. Removing a source
  applies at once, in place, with a `grant.narrowed` on their log.
* Pointing an agent's grant at a collection (or back to the default) follows the
  same rule.

Only the person does any of this; agents never see collections.
"""
from __future__ import annotations

import json

from django.db import transaction

from . import hcp
from .models import MemoryCollection, Person, PersonFact, PersonGrant


class NeedsConfirm(Exception):
    """A widening the person has not confirmed: `grants` are the live grants it
    would widen, and `sources` what each would newly reach."""

    def __init__(self, grants: list[PersonGrant], added: list[str]):
        self.grants, self.added = grants, added
        names = sorted({g.client_name for g in grants})
        super().__init__(f"this will also let {_and(names)} use {hcp.reach_label(added)}; "
                         f"confirm by listing these grants in confirm_widening")


class InUse(Exception):
    """A collection some live grant still uses cannot be removed."""


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def contexts(person: Person) -> list[str]:
    """The sources a collection of this person's may hold: `personal`, and the
    organisation of every workspace they belong to or have entries in."""
    slugs: set[str] = set()
    if person.user_id:
        from apps.workspaces import services as wsvc

        slugs |= wsvc.user_workspace_slugs(person.user)
    slugs |= set(PersonFact.objects.filter(person=person, workspace__isnull=False)
                 .values_list("workspace_id", flat=True).distinct())
    roots = sorted({hcp.root_of(s) for s in slugs if s})
    return [hcp.PERSONAL_SOURCE, *[hcp.tree_source(r) for r in roots]]


def _clean(person: Person, sources) -> list[str]:
    allowed = contexts(person)
    out = list(dict.fromkeys(str(s) for s in (sources or [])))
    bad = [s for s in out if s not in allowed]
    if bad:
        raise hcp.malformed(f"not a context of yours: {bad}; choose from {allowed}")
    if not out:
        raise hcp.malformed("a collection holds at least one context")
    return out


def _live_grants(person: Person, **filters) -> list[PersonGrant]:
    return list(PersonGrant.objects.select_related("agent", "collection")
                .filter(person=person, status=PersonGrant.ACTIVE, hcp_client__isnull=True,
                        agent__isnull=False, **filters))


def _check_confirm(widened: list[PersonGrant], added: list[str], confirm) -> None:
    want = {hcp.entry_urn(g.grant_id) for g in widened}
    if widened and not want <= {str(c) for c in (confirm or [])}:
        raise NeedsConfirm(widened, added)


def _apply(grant: PersonGrant, sources: list[str], collection, *, actor: hcp.Actor,
           why: str) -> None:
    """Re-resolve one live grant in place: a widening is a `grant.issued` (the
    person's confirmed act); a narrowing a `grant.narrowed`."""
    before = hcp.reached(hcp.grant_sources(grant))
    grant.restrictions = hcp.restrictions_for(list(grant.scopes or []), sources)
    grant.collection = collection
    grant.save(update_fields=["restrictions", "collection"])
    after = hcp.reached(sources)
    if after == before:
        return                              # what it reaches is unchanged
    event = "grant.issued" if after - before else "grant.narrowed"
    hcp.audit(grant.person, event, actor=actor, grant=grant, workspace_slug=grant.workspace_id,
              detail=(f"client={grant.client_name}; key={grant.client_key}; type={grant.grant_type}; "
                      f"modality=canopy-memory-settings (the person's act: {why}); "
                      f"expires={hcp._iso(grant.expires_at) or 'never'}; "
                      f"scopes={' '.join(grant.scopes)}; "
                      f"restrictions={json.dumps(grant.restrictions)}; "
                      f"reach=only {hcp.reach_label(sources)}"))


@transaction.atomic
def create(person: Person, *, name: str, sources, actor: hcp.Actor) -> MemoryCollection:
    name = (name or "").strip()[:80]
    if not name:
        raise hcp.malformed("name the collection")
    c = MemoryCollection.objects.create(person=person, name=name, sources=_clean(person, sources))
    hcp.audit(person, "collection.created", actor=actor,
              detail=f"{name}: {hcp.reach_label(c.sources)}")
    return c


@transaction.atomic
def update(person: Person, collection: MemoryCollection, *, actor: hcp.Actor,
           name: str | None = None, sources=None, confirm_widening=None) -> MemoryCollection:
    """Rename and/or change what it holds. A source added widens every live grant
    using it — refused (`NeedsConfirm`) unless `confirm_widening` names each one."""
    if name is not None:
        if not name.strip():
            raise hcp.malformed("name the collection")
        collection.name = name.strip()[:80]
    added: list[str] = []
    if sources is not None:
        new = _clean(person, sources)
        added = [s for s in new if s not in collection.sources]
        grants = _live_grants(person, collection=collection)
        widened = [g for g in grants
                   if hcp.reached(new) - hcp.reached(hcp.grant_sources(g))] if added else []
        _check_confirm(widened, added, confirm_widening)
        removed = [s for s in collection.sources if s not in new]
        collection.sources = new
        collection.save()
        for g in grants:
            _apply(g, hcp.collection_sources(collection), collection, actor=actor,
                   why=(f"changed collection {collection.name!r}"
                        + (f", adding {hcp.reach_label(added)}" if added else "")
                        + (f", removing {hcp.reach_label(removed)}" if removed else "")))
    else:
        collection.save()
    hcp.audit(person, "collection.updated", actor=actor,
              detail=f"{collection.name}: {hcp.reach_label(collection.sources)}")
    return collection


@transaction.atomic
def delete(person: Person, collection: MemoryCollection, *, actor: hcp.Actor) -> None:
    """Removing a collection an agent still uses would change what that agent
    reaches behind the person's back, so it is refused until they move it."""
    users = _live_grants(person, collection=collection)
    if users:
        raise InUse(f"{_and(sorted({g.client_name for g in users}))} still use "
                    f"{collection.name!r}; point them elsewhere first")
    PersonGrant.objects.filter(collection=collection).update(collection=None)
    hcp.audit(person, "collection.deleted", actor=actor, detail=collection.name)
    collection.delete()


@transaction.atomic
def use_for_grant(person: Person, grant: PersonGrant, collection: MemoryCollection | None, *,
                  actor: hcp.Actor, confirm_widening=None) -> PersonGrant:
    """Point an agent at a collection (None = its default, its own organisation):
    `grant` and every other live grant the person gave that agent (a session's
    too, so it never shadows the choice). A widening needs `confirm_widening`
    naming each grant it widens."""
    if (grant.person_id != person.pk or grant.status != PersonGrant.ACTIVE
            or grant.hcp_client_id or not grant.agent_id):
        raise hcp.denied("not one of your live agent grants")
    if collection is not None and collection.person_id != person.pk:
        raise hcp.denied("not one of your collections")
    sources = (hcp.collection_sources(collection) if collection is not None
               else hcp.agent_sources(grant.agent))
    grants = _live_grants(person, agent=grant.agent)
    widened = [g for g in grants if hcp.reached(sources) - hcp.reached(hcp.grant_sources(g))]
    held = hcp.reached(hcp.grant_sources(grant))
    _check_confirm(widened, [s for s in sources if hcp.reached([s]) - held] or sources,
                   confirm_widening)
    why = (f"{grant.client_name} uses collection {collection.name!r}" if collection is not None
           else f"{grant.client_name} back to its default reach")
    for g in grants:
        _apply(g, sources, collection, actor=actor, why=why)
    grant.refresh_from_db()
    return grant


def collection_dict(c: MemoryCollection) -> dict:
    return {"id": c.pk, "name": c.name, "sources": list(c.sources),
            "reach": hcp.reach_label(c.sources),
            "used_by": [{"grant_id": hcp.entry_urn(g.grant_id), "agent": g.agent.slug,
                         "name": g.client_name, "type": g.grant_type}
                        for g in _live_grants(c.person, collection=c)],
            "updated_at": c.updated_at.isoformat() if c.updated_at else None}
