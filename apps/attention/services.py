"""What is waiting on a person, and what they have let go.

The list is ranked by what just happened, newest first — not as a queue. An
oldest-first queue floated whatever had gone stale to the top, where it stayed;
here a thing nobody has touched in `QUIET_AFTER` drops into `quiet`, a separate
fold a person can let go of in one move.
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from .models import LetGo
from .sources import gather
from .types import AttentionRow

#: Untouched this long, an item is quiet: still listed, but out of the way.
QUIET_AFTER = dt.timedelta(days=3)


def _hidden(item: AttentionRow, let_go: LetGo | None, now: dt.datetime) -> bool:
    if let_go is None:
        return False
    if let_go.until is not None and let_go.until > now:
        return True
    return let_go.until is None and item.last_activity_at <= let_go.let_go_at


def attention(request, *, slugs: set[str], now: dt.datetime | None = None) -> dict[str, list[AttentionRow]]:
    """`{"now": [...], "quiet": [...], "let_go": [...]}`, each newest first."""
    now = now or timezone.now()
    items = gather(request, slugs=slugs)
    let_gos = {g.key: g for g in LetGo.objects.filter(user=request.user, key__in=[i.key for i in items])}
    out: dict[str, list[AttentionRow]] = {"now": [], "quiet": [], "let_go": []}
    for item in sorted(items, key=lambda i: i.last_activity_at, reverse=True):
        if _hidden(item, let_gos.get(item.key), now):
            out["let_go"].append(item)
        elif now - item.last_activity_at > QUIET_AFTER:
            out["quiet"].append(item)
        else:
            out["now"].append(item)
    return out


def let_go(user, keys: list[str], *, days: int | None = None, now: dt.datetime | None = None) -> int:
    """Set `keys` aside for `user` — until something new happens on each, or for
    `days` when given. Re-letting-go moves the mark forward."""
    now = now or timezone.now()
    until = now + dt.timedelta(days=days) if days else None
    with transaction.atomic():
        for key in dict.fromkeys(keys):
            LetGo.objects.update_or_create(user=user, key=key, defaults={"let_go_at": now, "until": until})
    return len(set(keys))


def take_back(user, key: str) -> bool:
    """Undo a let-go: the item returns to the list."""
    return LetGo.objects.filter(user=user, key=key).delete()[0] > 0
