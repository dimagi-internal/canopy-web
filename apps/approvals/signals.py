"""Wiring only: a task that needs a decision is delivered on commit; a decision settles the copies."""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.agents.models import AgentTask, AgentTaskAction

from . import cards, delivery


@receiver(post_save, sender=AgentTask, dispatch_uid="approvals_task_saved")
def _task_saved(sender, instance: AgentTask, **kwargs) -> None:
    if cards.needs_decision(instance):
        pk = instance.pk
        transaction.on_commit(lambda: delivery.deliver_safely(pk))


@receiver(post_save, sender=AgentTaskAction, dispatch_uid="approvals_action_saved")
def _action_saved(sender, instance: AgentTaskAction, **kwargs) -> None:
    pk = instance.task_id
    transaction.on_commit(lambda: _settle_safely(pk))


def _settle_safely(task_id: int) -> None:
    try:
        delivery.settle(task_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("approvals: settle failed for task %s", task_id)
