"""Domain signals emitted by the harness write path.

`turn_events_appended` fires AFTER an append commits (via transaction.on_commit)
so a subscriber (apps/realtime) can fan out durable events without racing the DB.

It exists because `services.append_events` uses bulk_create, which does NOT emit
post_save — a post_save receiver on TurnEvent would silently never fire. Framework
consumers connect to this instead.
"""
from __future__ import annotations

from django.dispatch import Signal

# Sent with: sender=Turn, turn=<Turn>, rows=<list[TurnEvent]> (the newly appended
# rows, in seq order). Fired post-commit.
turn_events_appended = Signal()

# Sent with: sender=Runner, runner=<Runner> after a runner's session report commits.
# apps/realtime fans the runner-owner's visible sessions to their supervisor group so
# the phone reflects live emdash activity instantly (one broadcast, N viewers) instead
# of every client polling. Post-commit, same reasoning as turn_events_appended.
sessions_reported = Signal()

# Sent with: sender=RunnerBinding, session_id=<uuid>, menu=<dict|None> for each
# session whose blocked-agent dialog appeared, changed or cleared in a report.
# Post-commit. The realtime frame and the phone push are sent inline in the
# report path; this is for consumers outside it (apps/slack posts the question
# into the thread the conversation started in).
session_menu_changed = Signal()


# --- page invalidation -------------------------------------------------------
#
# The other direction: not a signal the harness EMITS, but a receiver on its own
# rows, so a page showing the inbox is told when the inbox moves. The argument
# for a receiver rather than a call in each mutating service is written out in
# `apps/projects/signals.py` — there are many ways an Item changes (a decision
# over REST, a schedule nag raised by a turn finishing, a fleet audit creating a
# batch, `resolve_schedule_nags` dismissing one) and "remember to notify" at each
# of them is N sites that rot.
#
# Framework importing framework, so no boundary question arises here; `projects`
# has the harder version of this problem and its docstring explains the split.

from django.db.models.signals import post_delete, post_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402

from apps.canopy_sessions.invalidation import mark_dirty  # noqa: E402

from .models import Item  # noqa: E402

#: The resource URI the open-item collection belongs to.
#:
#: Collection-level for the same reason `INSIGHT_RESOURCE` is: a page showing a
#: filtered inbox still needs to know the SET changed, and per-row URIs would
#: have it subscribe only to rows it already has — which is exactly the rows
#: whose disappearance it can already see.
ITEM_RESOURCE = "item://"


@receiver([post_save, post_delete], sender=Item)
def _item_changed(sender, instance: Item, **kwargs) -> None:
    """Mark the item collection dirty when any item row moves.

    Deliberately NOT filtered to `state=OPEN`. A decision moves a row OUT of the
    open set, which is precisely the change a page showing that set must hear
    about — filtering on the post-save state would drop the transition that
    matters and keep the one that does not.

    `mark_dirty` coalesces per transaction, so `create_items` committing a fleet
    audit's whole batch sends one notification, not one per item. That is the
    same batching `apps/push` relies on, for the same reason.
    """
    mark_dirty(ITEM_RESOURCE)
