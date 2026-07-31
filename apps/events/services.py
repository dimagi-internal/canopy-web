"""Request-free service layer, so REST and (later) MCP share one implementation.

Mirrors ``apps/feedback/services.py`` and ``apps/harness/schedule_services.py`` —
the pattern that keeps two surfaces from drifting.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.events.models import Event

_FIELDS = ("source", "kind", "level", "key", "summary", "payload")


def record(items: list[dict], *, workspace) -> dict:
    """Write events, coalescing repeats of ``(workspace, source, key)``.

    The whole batch commits in one transaction, so a runner reporting six
    failure streaks in one tick lands atomically rather than half-writing.

    Coalescing is what makes this survivable as a log: a permanently-stuck
    transcript flush would otherwise write a row every tick forever. One row
    with ``count=400`` says "still broken after 400 attempts" better than 400
    rows do, and it is the same signal ``failure_log`` already computes on the
    runner — just made durable and fleet-visible.

    A blank ``key`` never coalesces. Two independent actions are two events.
    """
    created = 0
    coalesced = 0

    with transaction.atomic():
        for raw in items:
            data = {k: raw.get(k) for k in _FIELDS if raw.get(k) is not None}
            source = (data.get("source") or "").strip()
            if not source:
                # A row nobody can attribute is noise in a pool someone has to
                # read. Reject it here rather than storing an orphan.
                continue
            data["source"] = source
            key = (data.get("key") or "").strip()
            data["key"] = key

            if key:
                # F() so two runners reporting the same streak in the same
                # instant both count, rather than one clobbering the other with
                # a read-modify-write.
                bumped = Event.objects.filter(
                    workspace=workspace, source=source, key=key
                ).update(
                    count=F("count") + 1,
                    last_seen_at=timezone.now(),
                    level=data.get("level") or Event.INFO,
                    kind=data.get("kind") or "",
                    summary=data.get("summary") or "",
                    payload=data.get("payload") or {},
                )
                if bumped:
                    coalesced += 1
                    continue

            Event.objects.create(workspace=workspace, **data)
            created += 1

    return {"created": created, "coalesced": coalesced}


def list_events(
    *,
    workspace_slugs,
    source: str | None = None,
    kind: str | None = None,
    level: str | None = None,
    since: dt.datetime | None = None,
    limit: int = 100,
):
    """The pool, scoped to workspaces the caller belongs to.

    ``workspace_slugs`` is required and never defaulted — the whole point of the
    NOT NULL tenant FK is that there is no "no workspace" case to fall open on.
    """
    qs = Event.objects.filter(workspace_id__in=workspace_slugs)
    if source:
        # Prefix match so `?source=runner` returns every runner subsystem
        # without the caller knowing the full dotted name.
        qs = qs.filter(source__startswith=source)
    if kind:
        qs = qs.filter(kind__startswith=kind)
    if level:
        qs = qs.filter(level=level)
    if since:
        qs = qs.filter(last_seen_at__gte=since)
    return qs[: max(1, min(limit, 500))]


def prune(*, older_than: dt.timedelta) -> int:
    """Drop events untouched for longer than ``older_than``. Returns the count.

    Anchored on ``last_seen_at``, not ``first_seen_at``: a fault that started
    three weeks ago and is STILL happening is the most interesting row in the
    table, and pruning by when it began would delete exactly that one.
    """
    cutoff = timezone.now() - older_than
    deleted, _ = Event.objects.filter(last_seen_at__lt=cutoff).delete()
    return deleted
