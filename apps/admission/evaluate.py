"""Where admission rules are evaluated, and what a rejection does.

Two points, both before any session exists:

* **Enqueue** (`at_enqueue`, called from `harness.services.enqueue_turn` beside
  `_apply_capability`): rules that need no runner — no `turn_mode` and no
  `runner_kind` condition — reject here, earliest.
* **Claim** (`at_claim`, called from `harness.claim.claim_next_turn` right after
  `turn_mode.for_turn`): the only point where the runner's kind and the turn's
  resolved mode are both known. Both claim channels (HTTP and the runner
  socket) pass through it. Every rule is evaluated here, so one added after a
  turn was queued still applies.

Precedence: an enabled `allow` rule that matches exempts the turn from every
`reject` rule (that is how one agent opts out of a workspace-wide default). At
enqueue a condition that is not known yet counts as matching for an `allow`
and as not matching for a `reject`, so a turn an `allow` might cover waits for
the claim, where everything is known. Among matching rejects the most specific
wins, then the oldest. A DISABLED rule matches nothing — in either direction.

Rules apply to agent turns (an agent's own, or its chat's). A project turn has
no agent and is never evaluated.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import AdmissionRule

logger = logging.getLogger(__name__)

_PRINCIPAL_FOR_INITIATOR = {
    "user": AdmissionRule.MEMBER,
    "contact": AdmissionRule.CONTACT,
    "agent": AdmissionRule.AGENT,
}

#: Event kinds, on the turn's ledger payload and the workspace event log.
REJECTED_EVENT = "admission.rejected"


def _agent_of(turn):
    if turn.agent_id:
        return turn.agent
    if turn.chat_session_id and turn.chat_session.agent_id:
        return turn.chat_session.agent
    return None


def facts(turn, *, runner=None, mode: str | None = None) -> dict:
    """The turn, in the vocabulary rules filter on. None = not known yet."""
    return {
        "origin": turn.origin,
        "principal": _PRINCIPAL_FOR_INITIATOR.get(turn.initiator_kind or "", AdmissionRule.SYSTEM),
        "access": AdmissionRule.CONFINED if turn.capability else AdmissionRule.FULL,
        "capability": turn.capability or "",
        "turn_mode": mode,
        "runner_kind": runner.kind if runner is not None else None,
    }


def match(rule: AdmissionRule, known: dict) -> bool | None:
    """True / False, or None when a condition the rule sets is not known yet."""
    unknown = False
    for field in AdmissionRule.CONDITIONS:
        want = getattr(rule, field)
        if not want:
            continue
        have = known.get(field)
        if have is None:
            unknown = True
        elif have != want:
            return False
    return None if unknown else True


def decide(turn, agent, known: dict) -> AdmissionRule | None:
    """The reject rule that refuses this turn, or None."""
    rules = list(AdmissionRule.objects.filter(
        workspace_id=agent.workspace_id, enabled=True,
    ).filter(Q(agent__isnull=True) | Q(agent=agent)))
    if any(r.action == AdmissionRule.ALLOW and match(r, known) is not False for r in rules):
        return None
    rejects = [r for r in rules if r.action == AdmissionRule.REJECT and match(r, known) is True]
    if not rejects:
        return None
    return sorted(rejects, key=lambda r: (-r.specificity, r.pk))[0]


def at_enqueue(turn) -> bool:
    """Reject a just-created QUEUED turn on a rule that needs no runner. True
    if it was rejected. Called inside the transaction that created the turn,
    so no runner can claim it before this decides."""
    if turn.status != turn.QUEUED:
        return False
    agent = _agent_of(turn)
    if agent is None:
        return False
    rule = decide(turn, agent, facts(turn))
    return rule is not None and reject(turn, rule)


def at_claim(turn, runner, mode: str) -> bool:
    """Reject a candidate turn this runner is about to claim. True if it was
    rejected (the caller moves on to its next candidate)."""
    agent = _agent_of(turn)
    if agent is None:
        return False
    rule = decide(turn, agent, facts(turn, runner=runner, mode=mode or ""))
    return rule is not None and reject(turn, rule, runner=runner)


def result_note(rule: AdmissionRule) -> str:
    from .rules import summary

    return f"rejected: admission rule {rule.pk} ({summary(rule)}) — {rule.reason}"


def reject(turn, rule: AdmissionRule, *, runner=None) -> bool:
    """Close a QUEUED turn CANCELLED on `rule`, record it, and owe its notices.

    CANCELLED, not FAILED: nothing went wrong, a rule decided, and a FAILED
    turn would start a debugger turn (`harness.auto_debug`). Guarded on the
    status so a turn another runner claimed in the meantime is left alone.
    """
    from apps.harness.ledger import append_events
    from apps.harness.models import Turn

    note = result_note(rule)
    now = timezone.now()
    updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
        status=Turn.CANCELLED, finished_at=now, result_note=note,
    )
    if not updated:
        return False
    turn.refresh_from_db()
    AdmissionRule.objects.filter(pk=rule.pk).update(fired_count=F("fired_count") + 1, last_fired_at=now)
    stage = {"runner": runner.name, "runner_kind": runner.kind} if runner is not None else {"at": "enqueue"}
    append_events(turn, [{"kind": "status", "payload": {
        "status": Turn.CANCELLED, "result_note": note, "admission_rule": rule.pk, **stage,
    }}])
    _log(turn, rule, note, stage)
    turn_id, rule_id = turn.pk, rule.pk

    def _notify():
        from . import notices

        notices.deliver(turn_id, rule_id)

    transaction.on_commit(_notify)
    return True


def _log(turn, rule, note: str, stage: dict) -> None:
    """Best-effort line in the workspace event log: a log write must never
    fail the rejection it records."""
    try:
        from apps.events.services import record

        agent = _agent_of(turn)
        record([{
            "source": "admission", "kind": REJECTED_EVENT, "level": "info",
            "summary": f"{agent.slug}: turn {turn.pk} {note}"[:500],
            "payload": {"rule_id": rule.pk, "turn_id": str(turn.pk), "agent": agent.slug, **stage},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("admission: could not log the rejection of turn %s", turn.pk)
