"""Release a turn-bound lease the moment its turn ends.

A lease acquired with `turn_id` (the cloud runner exports CANOPY_TURN_ID to the
turn's process) belongs to that turn. Every status change of a turn is a
`status` row on its ledger, and the ledger's one writer fires
`turn_events_appended` after commit — so this receiver sees DONE / FAILED /
LOST / MISSED / CANCELLED however the turn got there (finish, cancel, the lease
sweep). `turn_status_changed` is not used: it fires only when a turn is
ENQUEUED (apps/harness/signals.py). The sweep re-checks bound turns too, so a
receiver that never ran still costs at most one sweep interval.
"""
from __future__ import annotations

import logging

from django.dispatch import receiver

from apps.harness.signals import turn_events_appended

logger = logging.getLogger(__name__)


@receiver(turn_events_appended, dispatch_uid="capabilities_release_on_turn_end")
def _release_on_turn_end(sender, turn, rows, **kwargs):
    from apps.harness.models import Turn

    if not any(getattr(r, "kind", "") == "status" and (r.payload or {}).get("status") in Turn.TERMINAL
               for r in rows):
        return
    try:
        from . import services

        services.release_for_turn(turn.pk)
    except Exception:  # noqa: BLE001 — finishing a turn must never depend on AWS
        logger.exception("capabilities: could not release leases of turn %s", turn.pk)
