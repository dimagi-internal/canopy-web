"""The personal-memory store's part of the cascade: an HCP entry learned from a session goes when the session does.

An entry says what it was learned from with an HCP v2 `derivedFrom` relationship naming a SOURCE
(`EntryRelationship`, `source_kind` "session", `source_ref` the session id: keyed like `BrainSource.kind/ref`).
Memory is about one person, so the session is removed for the entry when its words are gone (deleted, withdrawn,
the session gone), it is sealed, or the entry's own person excluded it or stopped agreeing to `memory`
(`cascade.removed_reason`, class `memory`, the entry's person as the subject). "Stopped" means they once agreed:
an entry an agent wrote with them before they ever answered the `memory` question is not a learning output the
class governs, so their silence does not delete it; their exclusion of the session does. An entry derived from several
sessions goes when any of them does: one statement cannot be re-derived from part of what fed it.

The entry is deleted through HCP's own path (`hcp.delete_entry`, as canopy, never the person): it is never served
to an agent again and its embedding is cleared; the person still sees it on their memory page, marked deleted, and
may hard-delete it there. Nothing here writes HCP's tables directly."""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from .models import MEMORY, BrainSource, LearningConsent

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


def sweep(source_ids=None) -> int:
    """Delete every live memory entry derived from a session now removed for its person. Returns entries deleted."""
    from apps.contacts import hcp  # noqa: PLC0415 — contacts imports this app
    from apps.contacts.models import EntryRelationship, PersonFact  # noqa: PLC0415 — as the other lazy imports here

    from .cascade import removed_reason  # noqa: PLC0415 — cascade calls this module

    live = PersonFact.objects.filter(superseded_at__isnull=True, retracted_at__isnull=True).values("entry_id")
    rels = EntryRelationship.objects.filter(type=EntryRelationship.DERIVED_FROM, source_kind=SESSION_KIND,
                                            entry_id__in=live).select_related("person")
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
        gone = False
        for rel in entry_rels:
            source = _source(rel.source_ref)
            why = "withdrawn" if source is None else removed_reason(source, MEMORY, subject, [])
            if why == "consent_ended" and not _once_agreed(subject):
                why = ""
            if why:
                gone = True
                break
        if not gone:
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


def _once_agreed(user_id) -> bool:
    """Did this person ever agree to `memory` (now or before a class removal or a revoke)?"""
    from apps.events.models import Event  # noqa: PLC0415 — as the other lazy imports here

    from . import consent  # noqa: PLC0415 — as the other lazy imports here

    if user_id is None:
        return False
    held = LearningConsent.objects.filter(user_id=user_id).values_list("classes", flat=True)
    if any(MEMORY in (c or []) for c in held):
        return True
    rows = Event.objects.filter(source=consent.SOURCE, kind__in=[consent.CONSENTED, consent.CLASS_ADDED, consent.CLASS_REMOVED],
                                payload__user_id=user_id).values_list("payload", flat=True)
    return any(MEMORY in (p.get("classes") or []) or p.get("class") == MEMORY for p in rows)
