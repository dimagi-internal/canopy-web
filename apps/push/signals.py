"""post_save receiver that marks an agent dirty when its waiting set may have
changed. Wiring only — whether to push is services.refresh_agent_waiting's call.

The waiting set is now a single source: open `Item`s. An Item is a real row, so
one receiver covers everything — no per-producer hops, no Drive-backed staleness
(the old gap when run gates were projected from a RunStore), and nothing to keep
in sync. The schedule nag is an Item too, so its raise/dismiss flows through here
for free. See 2026-07-21-supervisor-inbox-items-only-design.md.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.agents.models import AgentTask

from .services import mark_dirty

logger = logging.getLogger(__name__)


@receiver([post_save, post_delete], sender=AgentTask)
def _item_changed(sender, instance: AgentTask, **kwargs) -> None:
    mark_dirty(instance.agent_id)  # the FK shadow attribute — no query
