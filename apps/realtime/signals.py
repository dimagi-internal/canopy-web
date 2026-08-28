"""Fan-out receivers: turn the harness write path into live WS frames.

Mirrors apps/push/signals.py — a signal / post_save receiver schedules a
group_send on transaction.on_commit. Every publish is null-safe (see
groups.publish), so a realtime failure never breaks the write that triggered it.

Three sources, three frame types:
  - turn_events_appended (harness)       -> turn.{id}            "turn.event"
  - post_save Runner (harness)           -> supervisor.user.{id} "supervisor.runner"
  - post_save AgentWaitingSnapshot(push) -> supervisor.user.{id} "supervisor.waiting"

turn_events_appended is already sent post-commit (append_events fires it inside
its own on_commit), so its receiver publishes directly. The two post_save
receivers fire mid-transaction, so they defer their publish to on_commit.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.harness.models import Runner, Turn
from apps.harness.signals import sessions_reported, turn_events_appended
from apps.push.models import AgentWaitingSnapshot
from apps.workspaces.services import workspace_member_ids

from . import groups

# Ledger kinds that ALSO arrive on the session's transcript stream. Everything a
# runner tails out of the .jsonl — see stream_map.turn_event_to_frames for the
# aliases — as opposed to turn-lifecycle kinds (status / error / cancel), which
# no transcript ever contains.
TRANSCRIPT_ROW_KINDS = frozenset(
    {"user", "assistant", "tool_start", "tool_use", "tool_end", "tool_result"}
)


def _session_chat_events(turn, events):
    """The ledger events that still belong on the SESSION group.

    On a transcript-sourced session the chat rows are the transcript tailer's
    to ship: `/session-stream` sends every conversational record keyed by its
    durable composite ordinal, and the ledger's own copy of the same assistant
    text is a SECOND delivery under an id the client cannot reconcile with the
    first (`seq:<ordinal>` from the tailer vs `<turn8>:<ledger seq>` here,
    because a transcript-sourced session never projects Messages from a Turn,
    so `_resolve_message_id_sync` falls back to a synthetic id).

    The other row kinds survive that on their own — tool rows reconcile on
    `tool_use_id` and user rows on their text — but an assistant row has no
    correlation key at all, so both copies render and every reply doubles on
    screen while a reload shows it once. Observed live 2026-08-27 on the
    `targeting` session: four assistant lines, each on screen twice.

    Both runners have shipped `/session-stream` since the transcript became the
    durable source (spec 2026-07-24; runner/ec2 `_sync_session_streams` mirrors
    runner/canopy_runner `sync_session_streams`), which is what made the ledger
    copy redundant rather than merely noisy — nobody switched it off then.

    Ledger-sourced sessions (the dev stub, and pre-unification sessions that
    have not been reset) have no transcript to tail, so for them the ledger IS
    the only producer and every event goes through untouched.
    """
    # Function-local: keeps the chat app out of this module's import graph, as
    # the receiver below has always done.
    from apps.canopy_sessions.services import transcript_sourced

    session = turn.chat_session
    if session is None or not transcript_sourced(session):
        return events
    return [e for e in events if e.get("kind") not in TRANSCRIPT_ROW_KINDS]


@receiver(turn_events_appended, dispatch_uid="realtime_turn_events")
def _on_turn_events(sender, turn, rows, **kwargs):
    events = [groups.serialize_turn_event(row) for row in rows]
    group = groups.turn_group(turn.id)
    for event in events:
        groups.publish(group, {"type": "turn.event", "event": event})
    # A session turn also fans out to the per-session multiplayer group (SP3), so
    # every participant on the session socket sees the streamed response.
    if turn.chat_session_id:
        sgroup = groups.session_group(turn.chat_session_id)
        turn_id = str(turn.id)
        for event in _session_chat_events(turn, events):
            groups.publish(sgroup, {"type": "chat.turn_event", "event": event, "turn_id": turn_id})


@receiver(post_save, sender=Turn, dispatch_uid="realtime_runnable_wake")
def _on_turn_enqueued(sender, instance: Turn, created, **kwargs):
    """A newly-QUEUED turn wakes runners in its tenant so a blocked/idle runner
    claims it now instead of waiting out its poll interval. Coarse per-workspace
    wake — it only PROMPTS a claim; claim_next_turn still gates everything. Deferred
    to on_commit (create fires mid-transaction) and null-safe like every publish."""
    if not created or instance.status != Turn.QUEUED:
        return
    slug = groups.turn_workspace_slug(instance)
    if not slug:
        return
    transaction.on_commit(
        lambda: groups.publish(groups.runnable_group(slug), {"type": "runner.wake"})
    )


@receiver(post_save, sender=Runner, dispatch_uid="realtime_runner")
def _on_runner_saved(sender, instance: Runner, **kwargs):
    # A runner with no pairer has no user to notify (and no derivable tenant).
    if not instance.paired_by_id:
        return
    frame = {
        "type": "supervisor.runner",
        "runner": {
            "id": str(instance.id),
            "name": instance.name,
            "kind": instance.kind,
            "status": instance.live_status,
            "last_heartbeat_at": (
                instance.last_heartbeat_at.isoformat() if instance.last_heartbeat_at else None
            ),
        },
    }
    group = groups.supervisor_user_group(instance.paired_by_id)
    transaction.on_commit(lambda: groups.publish(group, frame))


@receiver(post_save, sender=AgentWaitingSnapshot, dispatch_uid="realtime_waiting")
def _on_waiting_saved(sender, instance: AgentWaitingSnapshot, **kwargs):
    agent = instance.agent
    if not agent.workspace_id:
        return
    frame = {
        "type": "supervisor.waiting",
        "agent": agent.slug,
        "waiting_count": instance.waiting_count,
    }
    member_ids = workspace_member_ids(agent.workspace)

    def _fire():
        for uid in member_ids:
            groups.publish(groups.supervisor_user_group(uid), frame)

    transaction.on_commit(_fire)


@receiver(sessions_reported, dispatch_uid="realtime_sessions_reported")
def _on_sessions_reported(sender, runner, **kwargs):
    """A runner reported its open sessions -> push the owner's visible sessions to
    their supervisor group. One broadcast reaches every device the user has open
    (phone + desktop + menubar) instead of each polling. Already post-commit (the
    sender fires inside its own on_commit), so publish directly."""
    if not runner.paired_by_id:
        return
    # Local imports keep this module import-cycle-free and the serialization co-located
    # with where the GET /sessions endpoint reads the same rows.
    from apps.harness.schemas import EmdashSessionOut
    from apps.harness.services import list_visible_sessions

    sessions = list_visible_sessions(runner.paired_by)
    frame = {
        "type": "supervisor.sessions",
        "sessions": [EmdashSessionOut.from_orm(s).model_dump(mode="json") for s in sessions],
    }
    groups.publish(groups.supervisor_user_group(runner.paired_by_id), frame)
