"""Stub executor — the SP2a stand-in for the SP2b cloud runner.

A real cloud runner will CLAIM a queued session turn (claim_next_turn), start it,
run `claude -p`, and append the real assistant/tool stream to the ledger. Until
that lands (it needs a deployed ECS service + a claude binary), this drives the
same transitions in-process with a canned reply, so the whole send -> Turn ->
execute -> ledger -> Message projection -> live tail loop is exercisable now.

It appends the SAME event kinds the real runner will, so nothing downstream (the
projection, SP1's realtime tail) has to change when the stub is swapped out.
"""
from __future__ import annotations

from django.utils import timezone

from apps.harness import services as harness_services
from apps.harness import ledger
from apps.harness.models import Turn


def execute_turn_stub(turn: Turn, *, reply: str = "(stub reply) received.") -> Turn:
    # Simulate claim+start. claim_next_turn does not route session turns yet
    # (SP2b), so drive queued -> claimed directly, then through the real
    # mark_running / append / finish services the runner will also use.
    updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
        status=Turn.CLAIMED, claimed_at=timezone.now()
    )
    turn.refresh_from_db()
    if not updated and turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        return turn  # already handled / not runnable

    harness_services.mark_running(turn, session_id=f"stub-{turn.id.hex[:8]}")
    ledger.append_events(turn, [{"kind": "assistant", "payload": {"text": reply}}])
    return harness_services.finish_turn(turn, status=Turn.DONE, result_note="stub")
