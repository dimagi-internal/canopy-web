"""Send a card to the people who decide it, once each, and keep every copy honest.

Order of reach: a push and a Slack DM for people who have those; an email only to someone
neither reached (so a person with a phone is not also mailed). Everything is idempotent — a
(task, person, channel) is delivered once, whatever re-saves the task — and a send that fails
never breaks the save that triggered it.
"""
from __future__ import annotations

import datetime as dt
import logging
from importlib import import_module

from django.utils import timezone

from apps.agents.models import AgentTask
from apps.push.models import NotificationPreference

from . import cards, deciders
from .models import ApprovalDelivery

logger = logging.getLogger(__name__)

#: channel -> module with send(task, user, card) -> ref | None  (and, optionally, settle(delivery, card)).
CHANNEL_MODULES = {
    ApprovalDelivery.PUSH: "apps.approvals.channel_push",
    ApprovalDelivery.SLACK: "apps.slack.approvals",
    ApprovalDelivery.EMAIL: "apps.approvals.channel_email",
}
FIRST_WAVE = (ApprovalDelivery.PUSH, ApprovalDelivery.SLACK)
BACKSTOP_WINDOW = dt.timedelta(hours=6)


def _send(channel: str, task, user, card: dict) -> dict | None:
    try:
        return import_module(CHANNEL_MODULES[channel]).send(task, user, card)
    except Exception:  # noqa: BLE001 — one channel failing must not stop the others
        logger.exception("approvals: %s send failed for %s", channel, task.key)
        return None


def wants(user) -> bool:
    pref = NotificationPreference.objects.filter(user=user).first()
    return pref is None or pref.approval_cards


def deliver(task_id: int) -> dict[str, int]:
    """Deliver the card for `task_id` to its deciders. Returns {channel: copies sent}."""
    task = (AgentTask.objects.select_related("agent", "created_by_agent", "created_by_user", "project", "workspace")
            .filter(pk=task_id).first())
    if task is None or not cards.needs_decision(task):
        return {}
    people = [u for u in deciders.for_task(task) if wants(u)]
    card = cards.build(task, sent_to=[u.get_full_name() or u.email for u in people])
    sent = {c: 0 for c in ApprovalDelivery.CHANNELS}
    for user in people:
        have = set(ApprovalDelivery.objects.filter(task=task, user=user).values_list("channel", flat=True))
        for channel in FIRST_WAVE:
            if channel in have:
                continue
            ref = _send(channel, task, user, card)
            if ref is not None:
                ApprovalDelivery.objects.get_or_create(task=task, user=user, channel=channel,
                                                       defaults={"ref": ref})
                have.add(channel)
                sent[channel] += 1
        if not have:
            ref = _send(ApprovalDelivery.EMAIL, task, user, card)
            if ref is not None:
                ApprovalDelivery.objects.get_or_create(task=task, user=user, channel=ApprovalDelivery.EMAIL,
                                                       defaults={"ref": ref})
                sent[ApprovalDelivery.EMAIL] += 1
    return sent


def deliver_safely(task_id: int) -> None:
    try:
        deliver(task_id)
    except Exception:  # noqa: BLE001 — a notification never costs the save that raised it
        logger.exception("approvals: delivery failed for task %s", task_id)


def deliver_missed(now=None) -> int:
    """The backstop: recent cards nobody has been told about yet."""
    now = now or timezone.now()
    since = now - BACKSTOP_WINDOW
    recent = (AgentTask.objects.filter(updated_at__gte=since)
              .filter(status=AgentTask.SUGGESTED)
              .exclude(approval_deliveries__isnull=False).order_by("pk")[:50])
    n = 0
    for task in recent:
        if cards.needs_decision(task):
            deliver_safely(task.pk)
            n += 1
    return n


def settle(task_id: int) -> int:
    """Bring every sent copy up to date once the card is decided. Returns copies updated."""
    task = (AgentTask.objects.select_related("agent", "created_by_agent", "created_by_user", "workspace")
            .filter(pk=task_id).first())
    if task is None or cards.needs_decision(task):
        return 0
    card = cards.build(task)
    n = 0
    for d in ApprovalDelivery.objects.filter(task=task, settled_at__isnull=True).select_related("user"):
        try:
            mod = import_module(CHANNEL_MODULES[d.channel])
            if hasattr(mod, "settle"):
                mod.settle(d, card)
        except Exception:  # noqa: BLE001 — the decision stands; a stale copy is a courtesy lost
            logger.exception("approvals: settle failed for %s via %s", task.key, d.channel)
            continue
        ApprovalDelivery.objects.filter(pk=d.pk).update(settled_at=timezone.now())
        n += 1
    return n
