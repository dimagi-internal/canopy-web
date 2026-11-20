"""The personal-memory store's part of the cascade: an HCP entry learned from a session goes when the session does.

An entry says what it was learned from with an HCP v2 `derivedFrom` relationship naming a SOURCE
(`EntryRelationship`, `source_kind` "session", `source_ref` the session id: keyed like `BrainSource.kind/ref`).
Memory is about one person, so the session is removed for the entry by `cascade.removed_reason` (class `memory`,
the entry's person as the subject). The cascade acts on an entry only when:

* the entry's person is a canopy USER who once agreed to `memory` (`cascade.ever_agreed`): then any removal counts
  (the words gone, the session sealed, their exclusion, their consent ended or narrowed); or
* whoever it is about, the session's words are gone (deleted, withdrawn, the session gone), or the person excluded
  the session themselves.

So an entry an agent wrote about a user before they ever answered the `memory` question is not a learning output
the class governs: neither their silence nor a seal (a sensitive session, a contact's) deletes it. A CONTACT's
entries (a person with no canopy account, e.g. a preference recorded through trusted contacts) are never touched
by the brain cascade. An entry derived from several sessions goes when any of them does: one statement cannot be
re-derived from part of what fed it.

The entry is deleted through HCP's own path (`hcp.delete_entry`, as canopy, never the person): it is never served
to an agent again and its embedding is cleared; the person still sees it on their memory page, marked deleted, and
may hard-delete it there. Nothing here writes HCP's tables directly."""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from . import participants
from .models import MEMORY, BrainSource

logger = logging.getLogger(__name__)

SESSION_KIND = "session"
REASON = "a session it was learned from was removed from learning"


def _source(ref: str):
    """The brain's source for a session id, made on first ask; None when the session is gone."""
    from apps.canopy_sessions.models import Session  # noqa: PLC0415 — canopy_sessions imports this app

    from . import sources  # noqa: PLC0415 — as the other lazy imports here

    found = BrainSource.objects.filter(kind=BrainSource.SESSION, ref=ref).select_related("session").first()
    if found is not None:
        return found
    try:
        pk = uuid.UUID(str(ref))
    except ValueError:
        return None                                         # a ref that is no session id names no session
    session = Session.objects.filter(pk=pk).first()
    return sources.for_session(session) if session is not None else None


def sweep(source_ids=None, memo: dict | None = None) -> int:
    """Delete every live memory entry derived from a session now removed for its person (see the module
    docstring for whose and why). Returns entries deleted."""
    from apps.contacts import hcp  # noqa: PLC0415 — contacts imports this app
    from apps.contacts.models import EntryRelationship, PersonFact  # noqa: PLC0415 — as the other lazy imports here

    from .cascade import ever_agreed, removed_reason  # noqa: PLC0415 — cascade calls this module

    memo = {} if memo is None else memo
    live = PersonFact.objects.filter(superseded_at__isnull=True, retracted_at__isnull=True).values("entry_id")
    rels = EntryRelationship.objects.filter(type=EntryRelationship.DERIVED_FROM, source_kind=SESSION_KIND,
                                            entry_id__in=live, person__user__isnull=False).select_related("person")
    if source_ids is not None:
        refs = list(BrainSource.objects.filter(pk__in=list(source_ids), kind=BrainSource.SESSION)
                    .values_list("ref", flat=True))
        rels = rels.filter(source_ref__in=refs)
    by_entry = defaultdict(list)
    for rel in rels:
        by_entry[rel.entry_id].append(rel)
    deleted = 0
    for entry_id, entry_rels in by_entry.items():
        subject = entry_rels[0].person.user_id
        if subject is None:
            continue                                   # a contact's entry: never the brain cascade's to delete
        agreed = ever_agreed(subject, MEMORY, memo)
        if not any(_gone(_source(rel.source_ref), subject, agreed, removed_reason) for rel in entry_rels):
            continue
        fact = hcp.current(entry_id)
        if fact is None or fact.retracted_at is not None:
            continue
        try:
            hcp.delete_entry(fact, actor=hcp.SYSTEM, reason=REASON)
            deleted += 1
        except Exception:  # noqa: BLE001 — one entry must not stop the sweep
            logger.exception("could not delete memory entry %s", entry_id)
    return deleted


def _gone(source, subject: int, agreed: bool, removed_reason) -> bool:
    """Is this session removed for the entry? For someone who once agreed to `memory`, any removal; for anyone
    else only their own exclusion or the words being gone."""
    why = "withdrawn" if source is None else removed_reason(source, MEMORY, subject, [])
    if not why or agreed:
        return bool(why)
    if why in ("withdrawn", "excluded"):
        return True
    return why == "sealed" and participants.sealed_reason(source.session) == BrainSource.SEALED_DELETED
