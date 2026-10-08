"""The thread GUARD: a message turn may be created only inside its thread's limits.

Called from `harness.services.enqueue_turn` — the one place every producer of a
turn goes through (the REST route, MCP, schedules, dispatch, chat) — for a turn
tagged `origin_ref.kind == "thread_message"`, AFTER its idempotency check, so a
retry of a message that already exists returns that turn instead of a refusal.
It runs inside the creating transaction with the thread row locked, so two
messages racing for the same slot cannot both pass.

The limits are enforced here rather than trusted to the moderator: whoever
dispatches, a closed or expired thread takes no message, a thread takes at most
`max_messages`, only a participant speaks (and only as itself), and messages are
numbered strictly 1, 2, 3 … in the order they were sent.
"""
from __future__ import annotations

from django.utils import timezone
from ninja.errors import HttpError

from . import services
from .models import AgentThread


class ThreadRefused(HttpError):
    """A refused thread message. An `HttpError`, so any API route that reaches
    `enqueue_turn` answers with its status and reason as problem+json."""


def is_thread_message(origin_ref) -> bool:
    return isinstance(origin_ref, dict) and origin_ref.get("kind") == services.MESSAGE_KIND


def check_message(origin_ref: dict, target_slug: str) -> AgentThread:
    """Refuse (raise ThreadRefused) unless this message may be sent now. Call inside
    a transaction: the thread row is locked until it commits."""
    tid = str(origin_ref.get("thread") or "")
    thread = AgentThread.objects.select_for_update().filter(id=tid).first() if tid else None
    if thread is None:
        raise ThreadRefused(409, f"thread {tid or '(none)'} does not exist")
    if thread.status != AgentThread.OPEN:
        raise ThreadRefused(409, f"thread {thread.id} is {thread.status}")
    if timezone.now() >= thread.deadline_at:
        # The status stays open: closing (timed_out) is the moderator's call.
        raise ThreadRefused(409, f"thread {thread.id} passed its deadline")
    used = services.messages_used(thread.id)
    if used >= thread.max_messages:
        raise ThreadRefused(409, f"thread {thread.id} used its {thread.max_messages} messages")
    speaker = str(origin_ref.get("speaker") or "")
    if speaker not in thread.agents:
        raise ThreadRefused(422, f"{speaker or '(no speaker)'} is not a participant in thread {thread.id}")
    if speaker != target_slug:
        raise ThreadRefused(
            422, f"a thread message is spoken by its speaker: origin_ref.speaker is {speaker} "
                 f"but the turn is for {target_slug or '(no agent)'}")
    try:
        n = int(origin_ref.get("n"))
    except (TypeError, ValueError):
        n = 0
    if n != used + 1:
        raise ThreadRefused(409, f"thread {thread.id} is on message {used + 1}, not {origin_ref.get('n')!r}")
    return thread
