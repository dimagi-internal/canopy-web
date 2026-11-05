"""`Turn.resolved_context`: writing down what a turn ran as.

Design: `docs/superpowers/specs/2026-11-05-turn-context-design.md` § 5. Written at each claim, where a lapsed lease's
re-claim overwrites it so it holds the run that last happened, when a direct session opens, and when a turn is refused
or rejected before it runs.

**A record, never a decision, so it can never stop or widen a turn.** What a turn may do is its stamp
(`Turn.capability`), already written; the runner's confinement reads that through the envelope. If building this
raises, `record` logs it, stores `error` in place of the snapshot and returns: the claim goes on with its stamp, and the
last run's snapshot is not left to pass for this one. A database error stays inside its own savepoint, so it cannot
poison a transaction the caller is in.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .models import Turn

logger = logging.getLogger(__name__)

#: When it was written: a runner claimed it, it was refused on the way in, a direct session opened.
CLAIM, ENQUEUE, OPEN = "claim", "enqueue", "open"

#: What `error` says when the snapshot could not be built.
NOT_BUILT = "canopy could not record what this turn ran as"


def rejected(rule, *, stage: dict | None = None, mode: str = "") -> dict:
    """An admission rule closed the turn after the Turn Context let it through (§ 3.3). `mode`: the one it would
    have run in, known at the claim, as the turn itself never gets stamped with it."""
    from apps.admission.rules import summary  # noqa: PLC0415 — admission imports this package

    return {"outcome": "rejected", "rule": rule.pk, "summary": summary(rule), "stage": dict(stage or {}), "mode": mode}


def raised_to_auto(rule) -> dict:
    from apps.admission.rules import summary  # noqa: PLC0415 — admission imports this package

    return {"outcome": "raised_to_auto", "rule": rule.pk, "summary": summary(rule)}


def _store(turn, doc: dict) -> None:
    # `update`, not `save`: not one of the turn's own transitions, so none of its receivers should see it.
    Turn.objects.filter(pk=turn.pk).update(resolved_context=doc)
    turn.resolved_context = doc


def record(turn, moment: str, *, context=None, runner=None, requested: str | None = None,
           admission: dict | None = None) -> None:
    """Write what `turn` ran as. `context`: the answer, when the caller holds a refusal; else it is read from the
    turn's stamp, and left on the turn so the envelope built from this same claim asks only once. Never raises."""
    from apps.agents import context_views, turn_context  # noqa: PLC0415 — agents imports this package

    now = timezone.now()
    try:
        with transaction.atomic():
            built = context if context is not None else turn_context.of_turn(turn, turn.acting_agent, runner=runner)
            _store(turn, context_views.snapshot(built, moment=moment, at=now, runner=runner, requested=requested,
                                                admission=admission))
    except Exception:  # noqa: BLE001 — a record of the turn is never worth the turn
        logger.exception("could not record what turn %s ran as", turn.pk)
        _record_the_failure(turn, moment, now)
        return
    if context is None:
        turn._turn_context = built


def _record_the_failure(turn, moment: str, now) -> None:
    try:
        with transaction.atomic():
            _store(turn, {"version": 1, "moment": moment, "recorded_at": now.isoformat(), "error": NOT_BUILT})
    except Exception:  # noqa: BLE001
        logger.exception("could not record that turn %s went unrecorded", turn.pk)
