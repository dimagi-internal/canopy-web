"""Two receivers that keep a lease from outliving what it belongs to.

**A turn-bound lease is released the moment its turn ends.** A lease acquired
with `turn_id` (the cloud runner exports CANOPY_TURN_ID to the turn's process)
belongs to that turn. Every status change of a turn is a `status` row on its
ledger, and the ledger's one writer fires `turn_events_appended` after commit —
so this receiver sees DONE / FAILED / LOST / MISSED / CANCELLED however the turn
got there (finish, cancel, the lease sweep). `turn_status_changed` is not used:
it fires only when a turn is ENQUEUED (apps/harness/signals.py). The sweep
re-checks bound turns too, so a receiver that never ran still costs at most one
sweep interval.

**A lease row deleted while it holds a machine terminates the machine first.**
The lease's agent, workspace and capability FKs CASCADE, so deleting any of them
deletes its leases; without this the machine would run on with no row to lead
the sweep to it (the stack's backstop Lambda would end it at
MaxInstanceLifetimeSeconds). Django sends `pre_delete` for every cascaded row.
"""
from __future__ import annotations

import logging

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.harness.signals import turn_events_appended

from .models import CapabilityLease

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


@receiver(pre_delete, sender=CapabilityLease, dispatch_uid="capabilities_terminate_on_lease_delete")
def _terminate_on_delete(sender, instance: CapabilityLease, **kwargs):
    from . import services

    services.terminate_for_deleted_row(instance)
