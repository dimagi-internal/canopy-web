"""Push-miss detection: the 300s poll audits the push path.

The poll is no longer the delivery mechanism, but it is still a second,
INDEPENDENT path to the same discovery — which makes it a free oracle for push
health. A message the poll found is a message push failed to ring for. That is a
direct observation of the failure, not a probe that can itself be wrong or a
heuristic about whether Pub/Sub "looks up".

Note this app DOES have signals, unlike `apps.events` (which must stay inert).
The distinction is what the receiver does: this one writes a log row, it never
creates work.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.harness.models import Turn
from apps.inbound import services
from apps.inbound.models import InboundMailbox

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Turn, dispatch_uid="inbound_push_miss")
def _on_turn_created(sender, instance: Turn, created: bool, **kwargs) -> None:
    """An email turn the POLL discovered, on a mailbox with a live watch, means
    push missed it."""
    if not created or instance.origin != Turn.ORIGIN_EMAIL:
        return
    ref = instance.origin_ref or {}
    if ref.get("discovered_by") != "poll":
        return

    mailbox = _mailbox_for(instance)
    if mailbox is None:
        # No row means we never asked for push on this mailbox, so there is
        # nothing to have missed. Silence here is correct.
        return
    if not mailbox.watch_expires_at or mailbox.watch_expires_at <= timezone.now():
        # No live watch: every poll-discovered message would be a "miss", and
        # logging them all would bury the real signal under the expected one.
        # The expired watch itself is already reported by note_watch_state.
        return

    try:
        services._record(
            mailbox.agent.workspace,
            kind="gmail.push.missed",
            level="error",
            key=mailbox.address,
            summary=(
                f"the 300s poll found mail on {mailbox.address} that push never "
                f"rang for — push is registered but not delivering"
            ),
            payload={
                "address": mailbox.address,
                "agent": mailbox.agent.slug,
                "turn_id": str(instance.id),
                "thread_id": ref.get("thread_id", ""),
                "last_push_at": (
                    mailbox.last_push_at.isoformat() if mailbox.last_push_at else ""
                ),
            },
        )
    except Exception:  # noqa: BLE001 — auditing must never break an enqueue
        logger.exception("push-miss audit failed for turn %s", instance.id)


def _mailbox_for(turn: Turn) -> InboundMailbox | None:
    if not turn.agent_id:
        return None
    return (
        InboundMailbox.objects.select_related("agent", "agent__workspace")
        .filter(agent_id=turn.agent_id, enabled=True)
        .first()
    )
