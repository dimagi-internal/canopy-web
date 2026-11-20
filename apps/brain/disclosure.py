"""What was read of a person's words: their disclosure summary.

Every read the brain allows leaves one `brain.read` row in the event log (`readers.record_read`, reads of one
session by one reader coalesced with a count). `compact` folds those rows into `DisclosureDay`: for each person
whose words were in the sources read, per learner and day, how many reads and how many distinct sources. A
person is not told about their own reads of their own words. Recomputed from the raw rows each time, so the
summary can always be checked against them (`tests/test_brain_disclosure.py`); ids and counts, never a word.
The day of a row is the day of its last read: coalescing keeps one count per (learner, reader, session)."""
from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from apps.events.models import Event

from .models import DisclosureDay, SourceParticipant
from .readers import EVENT_SOURCE


def raw_rows(person_ids=None) -> dict:
    """{(person_id, learner, day): (reads, {source ids})} straight from the `brain.read` events."""
    events = list(Event.objects.filter(source=EVENT_SOURCE).values("payload", "count", "last_seen_at"))
    wanted = {s for e in events for s in (e["payload"] or {}).get("sources", [])}
    people = defaultdict(set)
    for sid, uid in SourceParticipant.objects.filter(source_id__in=wanted, user__isnull=False).values_list(
            "source_id", "user_id"):
        people[sid].add(uid)
    out: dict = {}
    for e in events:
        p = e["payload"] or {}
        day = e["last_seen_at"].date()
        for sid in p.get("sources", []):
            for uid in people.get(sid, ()):
                if uid == p.get("reader_id") or (person_ids is not None and uid not in person_ids):
                    continue
                row = out.setdefault((uid, p.get("capability", ""), day), [0, set()])
                row[0] += e["count"]
                row[1].add(sid)
    return {k: (v[0], v[1]) for k, v in out.items()}


@transaction.atomic
def compact(person_ids=None) -> int:
    """Rebuild the summary rows (everyone, or `person_ids`) from the raw events. Returns rows written."""
    rows = raw_rows(person_ids)
    old = DisclosureDay.objects.all() if person_ids is None else DisclosureDay.objects.filter(person_id__in=person_ids)
    old.delete()
    DisclosureDay.objects.bulk_create([DisclosureDay(person_id=u, capability_slug=c, day=d, reads=r, sources=len(s))
                                       for (u, c, d), (r, s) in rows.items()])
    return len(rows)


def summary(user) -> list[dict]:
    """`user`'s disclosure days, newest first, freshly compacted."""
    compact([user.pk])
    return [{"day": d.day, "learner": d.capability_slug, "reads": d.reads, "sources": d.sources}
            for d in DisclosureDay.objects.filter(person=user).order_by("-day", "capability_slug")]
