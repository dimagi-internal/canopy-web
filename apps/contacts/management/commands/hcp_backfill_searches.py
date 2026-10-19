"""Rebuild a PersonSearch for every search that ran before searches were recorded.

    manage.py hcp_backfill_searches [--dry-run]

From the audit: each search wrote one `preference.read` per entry it returned
(in order), all sharing a `retrievalId` in `detail`. That gives who searched, when,
for which turn, and what came back in what place. The question is the audit's
`query=` when it has one, else — for a turn's own context search — the turn's
prompt, which is what that search used. Scores were not kept, so these are
`scored=False`: no meaning, nothing about what was left out. Idempotent: a
retrieval already recorded is skipped.
"""
from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from apps.contacts.models import PersonAuditEvent, PersonSearch
from apps.contacts.people import ENVELOPE_PURPOSE

_RETRIEVAL = re.compile(r"^retrievalId=urn:uuid:([0-9a-f-]{36})")
_WHY = re.compile(r"(?:^|; )why=([^;]*)")


def _field(detail: str, name: str) -> str:
    if name == "query":
        i = detail.find("; query=")
        return detail[i + 8:].strip() if i >= 0 else ""
    m = _WHY.search(detail.split("; query=")[0])
    return m.group(1).strip() if m else ""


class Command(BaseCommand):
    help = "Rebuild search records from the audit for searches before they were kept."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, dry_run: bool = False, **opts):
        known = {str(k) for k in PersonSearch.objects.values_list("retrieval_id", flat=True)}
        events = (PersonAuditEvent.objects.filter(event_type="preference.read", detail__startswith="retrievalId=")
                  .select_related("turn").order_by("person_id", "pk"))
        made = skipped = 0
        # Grouped by retrieval, not by adjacency: two searches at once interleave.
        searches: dict[str, list[PersonAuditEvent]] = {}
        for e in events.iterator():
            m = _RETRIEVAL.match(e.detail)
            if m:
                searches.setdefault(m.group(1), []).append(e)
        for rid, evs in searches.items():
            if rid in known:
                skipped += 1
                continue
            first = evs[0]
            query = _field(first.detail, "query")
            if not query and first.purpose == ENVELOPE_PURPOSE and first.turn is not None:
                query = first.turn.prompt or ""
            entries = [{"entry_id": str(e.entry_id), "position": i + 1, "because": _field(e.detail, "why")}
                       for i, e in enumerate(x for x in evs if x.entry_id)]
            made += 1
            if dry_run:
                continue
            PersonSearch.objects.create(
                person_id=first.person_id, retrieval_id=rid, created_at=first.timestamp,
                actor_id=first.actor_id, actor_type=first.actor_type, turn=first.turn,
                purpose=first.purpose, query=query[:4000], scored=False, entries=entries)
        self.stdout.write(f"{'would rebuild' if dry_run else 'rebuilt'} {made} searches; {skipped} already recorded")
