"""A runner's sync version: one number that moves whenever canopy-web records work a
box owes, so the box asks `GET /runners/{id}/sync` only when it moved.

The rows stay the truth (`RunnerBinding.pending_*`, `SessionHandoff`, `RunnerMint`,
`ListenSession`…) and a WS frame stays a doorbell. The version is the cheap
question in between: a box that missed a frame sees `v` move on its next
heartbeat, and a box that missed both finds the work on its periodic reconcile,
because `/sync` always answers every pending row, never a diff of them.

Rules:
- `bump` runs in the SAME transaction as the write that creates the work (call it
  inside the `atomic` block, never from `on_commit`), so a reader that sees the
  new version also sees the row.
- It is an `F()` increment, so two writers racing both move it; nothing else writes
  `Runner.sync_version` (`Runner.save` leaves it out).
- `/sync` reads the version BEFORE the rows: a bump that lands between the two
  makes the answer newer than its ETag, never older, and the next heartbeat's
  `v` asks again.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from django.db import transaction
from django.db.models import F

from .models import Runner


#: name -> fn(runner) -> list[dict]: work another app records for a box (`listen`),
#: answered under `sections` in `/sync`. Registered from that app's `ready`, since
#: harness never imports it.
SECTIONS: dict[str, Callable[[Runner], list[dict]]] = {}


def register_section(name: str, fn: Callable[[Runner], list[dict]]) -> None:
    SECTIONS[name] = fn


def bump(*runner_ids) -> None:
    """Move the sync version of each runner named (ids or rows; None is skipped)."""
    ids = {getattr(r, "pk", r) for r in _flat(runner_ids)}
    ids.discard(None)
    if ids:
        Runner.objects.filter(pk__in=ids).update(sync_version=F("sync_version") + 1)


def version_of(runner_id) -> int:
    """The current version, read from the row (never a cached instance)."""
    v = Runner.objects.filter(pk=getattr(runner_id, "pk", runner_id)).values_list(
        "sync_version", flat=True).first()
    return int(v or 0)


def etag(version: int) -> str:
    return f'"v{int(version)}"'


def _flat(items) -> Iterable:
    for item in items:
        if isinstance(item, (list, tuple, set, frozenset)):
            yield from item
        else:
            yield item


def ring_mint(runner_id) -> None:
    """The doorbell for a sign-in this box owes work on (`/mint/claim`), sent once the
    write commits. A doorbell only: the row is what the box acts on."""
    from apps.realtime import groups  # noqa: PLC0415 — realtime imports harness

    rid = getattr(runner_id, "pk", runner_id)
    if rid is not None:
        transaction.on_commit(lambda: groups.publish(groups.runner_group(rid), {"type": "runner.mint"}))
