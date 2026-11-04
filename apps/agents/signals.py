"""The task collection's page-invalidation receiver — a receiver on `AgentTask`, the row
this app owns, so it lives here and not beside the generic machinery in
`canopy_sessions.invalidation` nor in `apps/harness/signals.py`. Connected from
`AgentsConfig.ready`."""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.canopy_sessions.invalidation import mark_dirty

from .models import AgentTask

# --- page invalidation -------------------------------------------------------
#
# The other direction: not a signal the harness EMITS, but a receiver on its own
# rows, so a page showing the tasks waiting on you is told when that set moves.
#
# Why a receiver rather than a call in each mutating service: there are many ways
# a Task changes (an action over REST or MCP, a schedule nag raised by a turn
# finishing, a fleet audit creating a batch, `resolve_schedule_nags` closing
# one, the admin) and "remember to notify" at
# each of them is N sites that rot. This repo's own evidence for that is not
# theoretical: `page_tools.py` shipped with ten passing tests that nothing
# imported, and six tenancy predicates each independently grew a `NULL means
# allow` leg. One receiver on the real row cannot be forgotten by a future
# author. `apps/push/signals.py` made the same call for the same reason.
#
# Where such a receiver lives: in the app that OWNS the row, never beside the
# generic machinery in `canopy_sessions.invalidation`. Here that is framework
# importing framework, so no boundary question arises; a PRODUCT app that owns an
# invalidated resource puts its receiver in its own `signals.py`, because
# framework must never import product (ARCHITECTURE.md,
# `tests/test_architecture_boundary.py`).

#: The resource URI the task collection belongs to.
#:
#: MCP's vocabulary, not one of ours, so that when FastMCP grows a server-side
#: subscription API this string is already the thing an agent would subscribe
#: to. Collection-level: a page showing a filtered task list still needs to know the
#: SET changed, and per-row URIs would have it subscribe only to rows it already
#: has — which is exactly the rows whose disappearance it can already see.
TASK_RESOURCE = "task://"


@receiver([post_save, post_delete], sender=AgentTask)
def _task_changed(sender, instance: AgentTask, **kwargs) -> None:
    """Mark the task collection dirty when any task row moves.

    Deliberately NOT filtered to an open ask. An action moves a row OUT of the
    waiting set, which is precisely the change a page showing that set must hear
    about — filtering on the post-save state would drop the transition that
    matters and keep the one that does not.

    `mark_dirty` coalesces per transaction, so `create_tasks` committing a fleet
    audit's whole batch sends one notification, not one per task. That is the
    same batching `apps/push` relies on, for the same reason.
    """
    mark_dirty(TASK_RESOURCE)
