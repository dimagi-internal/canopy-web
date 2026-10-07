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

# Sent with: sender=Session, session_ids=<list[uuid]> for sessions that just became
# ARCHIVED because they were CLOSED — archived in emdash, closed from canopy, or
# absent from consecutive complete reports by their own runner. Never for a runner
# that merely went quiet. Post-commit. apps/slack tells a thread a session was
# shared into that its replies no longer reach anything.
sessions_closed = Signal()

# Sent with: sender=Session, session=<Session>, rows=<list[(index, kind, text)]>
# — the `user` and `assistant` text records of a runner's live transcript stream,
# for a batch that persisted something new. This is the half of a conversation
# the ledger never sees: someone typing straight into the agent's session on its
# box, and the agent writing AFTER its turn closed (emdash reads a background
# yield as the end of the turn, so a reply finished after waiting on CI lands
# here and nowhere else). apps/slack uses it to keep a Slack thread in step.
# Post-commit.
transcript_rows_streamed = Signal()

# Sent with: sender=Turn, turn=<Turn> the moment an ask is ENQUEUED and has a
# status worth telling somebody about. Post-commit.
#
# The other status transitions (claimed, running, done, failed) already append
# a `status` TurnEvent and so ride `turn_events_appended`; enqueue writes no
# event, and it is the single most important moment to report — it is the one
# where a person has just pressed send and is looking straight at the screen.
# Without it, "queued behind an offline runner" is indistinguishable from
# "working on it" for as long as nobody touches the turn, which on a closed
# laptop is forever.
#
# A signal rather than a call into each channel because there are five send
# paths (the chat socket, REST, a contact, Slack, email) and "remember to tell
# the channels" at each of them is five sites that rot — the same argument the
# page-invalidation receiver below makes.
turn_status_changed = Signal()


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

from django.db.models.signals import post_delete, post_save  # noqa: E402
from django.dispatch import receiver  # noqa: E402

from apps.canopy_sessions.invalidation import mark_dirty  # noqa: E402

from apps.agents.models import AgentTask  # noqa: E402

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


# -- provenance: every Turn and Session says what created it -----------------
#
# Receivers rather than a call at each creation site, for the reason the task
# receiver above gives: there are ten-odd sites (enqueue_turn, the close-out
# upsert, five raw `Session.objects.create`s, the runner report…) and the next
# one will not remember. `pre_save` fills what the site did not set from the
# request in flight; `post_save` logs ONE `TURN_CREATED` / `SESSION_CREATED`
# line on commit. Both no-ops for an update. See apps/harness/provenance.py.

from django.db.models.signals import pre_save  # noqa: E402

from apps.canopy_sessions.models import Session  # noqa: E402

from . import provenance  # noqa: E402
from .models import Turn  # noqa: E402


@receiver(pre_save, sender=Turn)
@receiver(pre_save, sender=Session)
def _stamp_provenance(sender, instance, raw=False, **kwargs) -> None:
    if raw:   # fixture loading: the row says what it says
        return
    try:
        provenance.stamp(instance)
    except Exception:  # noqa: BLE001 — a record must never fail a creation
        import logging

        logging.getLogger("canopy.provenance").exception("could not stamp provenance")


@receiver(post_save, sender=Turn)
@receiver(post_save, sender=Session)
def _log_created(sender, instance, created=False, raw=False, **kwargs) -> None:
    if created and not raw:
        provenance.on_created(instance)
