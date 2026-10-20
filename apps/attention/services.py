"""What is waiting on a person, and what they have hidden.

The list is ranked by what just happened, newest first — not as a queue. An
oldest-first queue floated whatever had gone stale to the top, where it stayed;
here a thing nobody has touched in `QUIET_AFTER` drops into `quiet`, a separate
fold a person can hide in one move.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from .models import Hidden
from .sources import gather
from .types import AttentionRow

#: Untouched this long, an item is quiet: still listed, but out of the way.
QUIET_AFTER = dt.timedelta(days=3)


def _hidden(item: AttentionRow, mark: Hidden | None, now: dt.datetime) -> bool:
    if mark is None:
        return False
    if mark.until is not None and mark.until > now:
        return True
    return mark.until is None and item.last_activity_at <= mark.hidden_at


def attention(request, *, slugs: set[str], now: dt.datetime | None = None) -> dict[str, list[AttentionRow]]:
    """`{"now": [...], "quiet": [...], "hidden": [...], "running": [...]}`, each
    newest first. `running` is never quiet or hidden: it is what is moving."""
    now = now or timezone.now()
    items = gather(request, slugs=slugs)
    marks = {g.key: g for g in Hidden.objects.filter(user=request.user, key__in=[i.key for i in items])}
    out: dict[str, list[AttentionRow]] = {"now": [], "quiet": [], "hidden": [], "running": []}
    for item in sorted(items, key=lambda i: i.last_activity_at, reverse=True):
        if item.running:
            out["running"].append(item)
        elif _hidden(item, marks.get(item.key), now):
            out["hidden"].append(item)
        elif now - item.last_activity_at > QUIET_AFTER:
            out["quiet"].append(item)
        else:
            out["now"].append(item)
    return out


def hide(user, keys: list[str], *, days: int | None = None, now: dt.datetime | None = None) -> int:
    """Hide `keys` from `user`'s list — until something new happens on each, or
    for `days` when given. Hiding again moves the mark forward."""
    now = now or timezone.now()
    until = now + dt.timedelta(days=days) if days else None
    with transaction.atomic():
        for key in dict.fromkeys(keys):
            Hidden.objects.update_or_create(user=user, key=key, defaults={"hidden_at": now, "until": until})
    return len(set(keys))


def unhide(user, key: str) -> bool:
    """Undo a hide: it returns to the list."""
    return Hidden.objects.filter(user=user, key=key).delete()[0] > 0
