"""Confirmation (HCP v2 3.8) and standing (4.7.1): what the person's yes or no does to an entry.

A confirmation is a governance act (1.6): the person, through a management client, never an agent.
It is appended to the version it was made on and changes neither the entry's declaration type nor its
asserter; what it gives the entry is *standing* in a conflict. v1's `userVerified` is derived from it.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from ..models import EntryConfirmation, EntryRelationship, PersonFact
from .core import Actor, audit, denied, malformed


def latest(fact: PersonFact) -> str | None:
    """The most recent confirmation disposition on this version, or None."""
    row = fact.confirmations.order_by("-pk").first()
    return row.disposition if row is not None else None


def is_confirmed(fact: PersonFact) -> bool:
    return latest(fact) == EntryConfirmation.CONFIRMED


def is_authoritative(fact: PersonFact) -> bool:
    """4.7.1: a `person-declared` entry recorded as authenticated, or one whose most recent
    confirmation is `confirmed`, whoever asserted it. Never from the declaration type alone: an
    agent's claim that the person said it is `unverified` until the person confirms it (3.7)."""
    last = latest(fact)
    if last is not None:
        return last == EntryConfirmation.CONFIRMED
    return fact.basis == PersonFact.DECLARED and fact.assurance == PersonFact.AUTHENTICATED


def render(fact: PersonFact) -> list[dict]:
    return [{"disposition": c.disposition, "confirmedBy": {"id": c.confirmed_by},
             "confirmedAt": c.confirmed_at.isoformat()} for c in fact.confirmations.all()]


def user_verified(fact: PersonFact) -> bool:
    """What v1's `provenance.userVerified` says: the person wrote it, or has confirmed this version."""
    return bool(fact.user_verified) or is_confirmed(fact)


def _live_partners(fact: PersonFact):
    """The current, live entries linked to this one by a `contradicts` relationship."""
    ids = list(EntryRelationship.objects.filter(
        person_id=fact.person_id, entry_id=fact.entry_id, type=EntryRelationship.CONTRADICTS,
        target_entry_id__isnull=False).values_list("target_entry_id", flat=True))
    return PersonFact.objects.filter(person_id=fact.person_id, entry_id__in=ids, superseded_at__isnull=True,
                                     retracted_at__isnull=True)


def still_in_conflict(fact: PersonFact) -> bool:
    """Does the entry still have a live counterpart it contradicts (active or itself held)?"""
    return _live_partners(fact).filter(status__in=[PersonFact.ACTIVE, PersonFact.CONFLICTED]).exists()


@transaction.atomic
def confirm(fact: PersonFact, disposition: str, *, actor: Actor) -> PersonFact:
    """Confirm or reject the entry's current version (3.8) and apply the transitions of 4.7.3."""
    if not actor.is_person:
        raise denied("only the person may do this")
    if disposition not in (EntryConfirmation.CONFIRMED, EntryConfirmation.REJECTED):
        raise malformed("disposition is confirmed or rejected")
    if fact.superseded_at is not None or fact.retracted_at is not None:
        raise denied()
    person, was_conflicted = fact.person, fact.status == PersonFact.CONFLICTED
    EntryConfirmation.objects.create(fact=fact, disposition=disposition, confirmed_by=actor.id[:200])
    audit(person, "context.confirmed", actor=actor, entry=fact,
          detail=f"disposition={disposition}; version={fact.version}")
    partners = list(_live_partners(fact))
    if disposition == EntryConfirmation.REJECTED:
        PersonFact.objects.filter(pk=fact.pk).update(
            status=PersonFact.DELETED, retracted_at=timezone.now(), embedding=None, embedding_model="")
        fact.refresh_from_db()
        if was_conflicted:
            audit(person, "conflict.resolved", actor=actor, entry=fact,
                  detail="transition=rejected: the entry was rejected")
        _release(partners, actor=actor, because=fact)
        return fact
    # confirmed: it now has standing.
    if was_conflicted:
        PersonFact.objects.filter(pk=fact.pk).update(status=PersonFact.ACTIVE)
        fact.status = PersonFact.ACTIVE
    for other in partners:
        # An authoritative counterpart is set aside by the person's acceptance, but an
        # attestation never is (4.4, 4.7.3): both stay, the conflict surfaced without a status change.
        if (other.status == PersonFact.ACTIVE and other.basis != PersonFact.ATTESTED
                and is_authoritative(other) and other.pk != fact.pk):
            PersonFact.objects.filter(pk=other.pk).update(status=PersonFact.DEPRECATED)
            audit(person, "conflict.resolved", actor=actor, entry=fact, related_entry_id=other.entry_id,
                  detail="transition=accepted: the entry the person confirmed outranks the one it contradicted")
    if was_conflicted:
        audit(person, "conflict.resolved", actor=actor, entry=fact,
              detail="transition=accepted: the person confirmed the entry")
    return fact


def release(partners, *, actor: Actor, because: PersonFact) -> None:
    _release(partners, actor=actor, because=because)


def _release(partners, *, actor: Actor, because: PersonFact) -> None:
    """A held counterpart returns to active when nothing else it contradicts is still live (4.7.3)."""
    for other in partners:
        if other.status != PersonFact.CONFLICTED:
            continue
        rest = [p for p in _live_partners(other) if p.entry_id != because.entry_id]
        if any(p.status in (PersonFact.ACTIVE, PersonFact.CONFLICTED) for p in rest):
            continue
        PersonFact.objects.filter(pk=other.pk).update(status=PersonFact.ACTIVE)
        audit(other.person, "conflict.resolved", actor=actor, entry=other, related_entry_id=because.entry_id,
              detail="transition=ended: nothing it contradicted remains")
