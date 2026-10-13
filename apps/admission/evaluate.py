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

* **Reach** (`reach` rules: no runner will take it). Known only by asking the
  routing (`harness.claim.turn_reach`), so it is asked only when such a rule is
  in scope — at enqueue, and again by `recheck` when the answer can change on
  its own: a runner paused open-ended (`recheck_stranded`, from
  `harness.services.pause`) and the passing of an offline rule's wait
  (`sweep_unrunnable`, on the heartbeat clock that also wakes paused runners).
  A claim never matches one: a runner claiming it is the answer.

* **Refusals** (`at_refusal`, canopy-web#1416): canopy refuses two kinds of turn
  on its own, before any rule — a blocked sender's mail
  (`harness.services._refused_email_turn`) and a person the agent offers
  nothing (`_apply_capability`). Those stay refused whatever the rules say:
  they are access decisions, and no admission rule can grant access. What a
  rule decides for them is what a rejection decides for any other turn — the
  ledger event, the fired count and the notices. Seeded as `blocked-sender`
  and `offered-nothing`, both with every notice off, which is what the
  refusals did before they were rows.
"""
from __future__ import annotations

import datetime as dt
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
#: How a rejected turn's `result_note` starts — what `harness.turn_status`
#: reads to tell a rejection from a person pressing stop.
REJECTED_PREFIX = "rejected:"
#: A parked turn with a resume time: it waits, and no rule rejects it.
HELD = "held"


def facts(turn, *, runner=None, mode: str | None = None) -> dict:
    """The turn, in the vocabulary rules filter on. None = not known yet.

    `reach` is "live" once a runner is claiming it (that runner is the answer)
    and unknown otherwise until `reach_facts` asks the routing."""
    return {
        "reach": "live" if runner is not None else None,
        "origin": turn.origin,
        "principal": _PRINCIPAL_FOR_INITIATOR.get(turn.initiator_kind or "", AdmissionRule.SYSTEM),
        # A blocked sender's turn never reaches `decide`; `at_refusal` says so.
        "sender": "",
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
        elif field == "reach" and want == AdmissionRule.OFFLINE \
                and known.get("waited_hours", 0) < rule.wait_hours:
            return False  # down, but not for as long as this rule waits
    return None if unknown else True


def _rules(agent) -> list[AdmissionRule]:
    return list(AdmissionRule.objects.filter(
        workspace_id=agent.workspace_id, enabled=True,
    ).filter(Q(agent__isnull=True) | Q(agent=agent)))


def reach_facts(turn) -> tuple[dict, str]:
    """Where the turn stands with the runners that could take it, as rule
    facts, plus the sentence naming them (who paused which box, when, why;
    or since when it was last heard from) for whoever is told."""
    from apps.harness import claim

    reach = claim.turn_reach(turn)
    if reach.kind == claim.PARKED:
        state = HELD if reach.held_until is not None else AdmissionRule.PAUSED
    elif reach.kind == claim.OFFLINE:
        state = AdmissionRule.OFFLINE
    else:
        state = reach.kind
    detail = ("; ".join(claim.runner_situation(r) for r in reach.runners) + "."
              if state in (AdmissionRule.PAUSED, AdmissionRule.OFFLINE) else "")
    waited = (timezone.now() - turn.created_at).total_seconds() / 3600
    return {"reach": state, "waited_hours": waited}, detail


def decide(turn, agent, known: dict, rules: list[AdmissionRule] | None = None) -> AdmissionRule | None:
    """The reject rule that refuses this turn, or None."""
    if rules is None:
        rules = _rules(agent)
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
    agent = turn.acting_agent
    if agent is None:
        return False
    rules = _rules(agent)
    known, detail = facts(turn), ""
    if any(r.reach for r in rules):
        reach, detail = reach_facts(turn)
        known.update(reach)
    rule = decide(turn, agent, known, rules)
    return rule is not None and reject(turn, rule, detail=detail)


def recheck(turn) -> bool:
    """Put a QUEUED turn to the reach rules again, now that where it stands may
    have changed without anything happening to the turn itself. True if it was
    rejected."""
    if turn.status != turn.QUEUED:
        return False
    agent = turn.acting_agent
    if agent is None:
        return False
    rules = _rules(agent)
    if not any(r.reach for r in rules):
        return False
    reach, detail = reach_facts(turn)
    rule = decide(turn, agent, {**facts(turn), **reach}, rules)
    return rule is not None and reject(turn, rule, detail=detail, stage={"at": "recheck"})


def recheck_stranded(runner) -> int:
    """A runner was just paused with no time to resume: re-check every queued
    turn it was one of the takers for. Returns how many were rejected. Never
    raises — the pause has happened either way."""
    from apps.harness.claim import queued_turns_covered_by

    rejected = 0
    try:
        for turn in queued_turns_covered_by(runner):
            with transaction.atomic():
                rejected += recheck(turn)
    except Exception:  # noqa: BLE001
        logger.exception("admission: re-checking turns stranded by pausing %s failed", runner.pk)
    return rejected


#: How often `sweep_unrunnable` actually looks, per process. It rides every
#: runner heartbeat; the waits it enforces are measured in hours.
SWEEP_EVERY = dt.timedelta(minutes=5)
_last_sweep: dt.datetime | None = None


def sweep_unrunnable(now: dt.datetime) -> int:
    """Re-check queued turns old enough for an enabled `reach: offline` rule's
    wait. Returns how many were rejected. Throttled to `SWEEP_EVERY`; never
    raises — it runs on a runner's heartbeat."""
    global _last_sweep
    if _last_sweep is not None and now - _last_sweep < SWEEP_EVERY:
        return 0
    _last_sweep = now
    from apps.harness.models import Turn

    rejected = 0
    try:
        waits = AdmissionRule.objects.filter(
            enabled=True, action=AdmissionRule.REJECT, reach=AdmissionRule.OFFLINE,
        ).values_list("wait_hours", flat=True)
        if not waits:
            return 0
        cutoff = now - dt.timedelta(hours=min(waits))
        for turn in Turn.objects.filter(status=Turn.QUEUED, created_at__lte=cutoff).select_related(
                "agent", "chat_session", "chat_session__agent").order_by("created_at"):
            with transaction.atomic():
                rejected += recheck(turn)
    except Exception:  # noqa: BLE001
        logger.exception("admission: sweeping unrunnable turns failed")
    return rejected


#: Event kind for a turn an `auto` rule raised.
AUTO_EVENT = "admission.auto"


def auto_rule(turn, runner, mode) -> AdmissionRule | None:
    """The enabled `auto` rule under which this runner claims `turn` in AUTO, or
    None. Only a MANUAL that is the agent's standing posture can be raised
    (`turn_mode.Resolved.raisable`): never an editor's cap, a dispatch that asked
    for manual, a session that started manual, or auto withheld from an
    unverified message. Pure; `record_auto` records it once the claim lands."""
    from apps.harness import turn_mode as modes

    if mode is None or mode.mode != modes.MANUAL or not mode.raisable:
        return None
    agent = turn.acting_agent
    if agent is None:
        return None
    known = facts(turn, runner=runner, mode=modes.MANUAL)
    autos = [r for r in _rules(agent) if r.action == AdmissionRule.AUTO and match(r, known) is True]
    return sorted(autos, key=lambda r: (-r.specificity, r.pk))[0] if autos else None


def auto_basis(rule: AdmissionRule) -> str:
    return f"admission rule {rule.pk}: {rule.reason}"[:320]


def record_auto(turn, rule: AdmissionRule, runner) -> None:
    """A claim under `rule` landed: count it and log it. Never raises."""
    try:
        AdmissionRule.objects.filter(pk=rule.pk).update(
            fired_count=F("fired_count") + 1, last_fired_at=timezone.now())
        _log_auto(turn, turn.acting_agent, rule, runner)
    except Exception:  # noqa: BLE001
        logger.exception("admission: could not record the auto mode of turn %s", turn.pk)


def _log_auto(turn, agent, rule, runner) -> None:
    try:
        from apps.events.services import record

        record([{
            "source": "admission", "kind": AUTO_EVENT, "level": "info",
            "summary": f"{agent.slug}: turn {turn.pk} runs in auto on {runner.name}: "
                       f"admission rule {rule.pk} — {rule.reason}"[:500],
            "payload": {"rule_id": rule.pk, "turn_id": str(turn.pk), "agent": agent.slug,
                        "runner": runner.name, "runner_kind": runner.kind},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("admission: could not log the auto mode of turn %s", turn.pk)


def at_claim(turn, runner, mode: str) -> bool:
    """Reject a candidate turn this runner is about to claim. True if it was
    rejected (the caller moves on to its next candidate)."""
    agent = turn.acting_agent
    if agent is None:
        return False
    rule = decide(turn, agent, facts(turn, runner=runner, mode=mode or ""))
    return rule is not None and reject(turn, rule, runner=runner)


def at_refusal(turn, *, sender: str = "", access: str = "") -> AdmissionRule | None:
    """canopy just refused `turn` on its own (it is already CANCELLED, with its
    reason in `result_note`): fire the refusal rule that covers it, if one is
    enabled. Returns that rule. Never raises — the refusal stands either way."""
    try:
        agent = turn.acting_agent
        if agent is None:
            return None
        known = {**facts(turn), "sender": sender}
        if access:
            known["access"] = access
        rules = [r for r in AdmissionRule.objects.filter(
            workspace_id=agent.workspace_id, enabled=True, action=AdmissionRule.REJECT,
        ).filter(Q(agent__isnull=True) | Q(agent=agent)).filter(
            Q(sender=AdmissionRule.BLOCKED) | Q(access=AdmissionRule.NONE))
            if match(r, known) is True]
        if not rules:
            return None
        rule = sorted(rules, key=lambda r: (-r.specificity, r.pk))[0]
        _fire(turn, rule, turn.result_note, {"at": "refusal"})
        return rule
    except Exception:  # noqa: BLE001 — the refusal is the safety; the record is not
        logger.exception("admission: could not record the refusal of turn %s", turn.pk)
        return None


def result_note(rule: AdmissionRule, detail: str = "") -> str:
    from .rules import summary

    why = f"{rule.reason} {detail}".strip()
    return f"{REJECTED_PREFIX} admission rule {rule.pk} ({summary(rule)}) — {why}"


def reject(turn, rule: AdmissionRule, *, runner=None, detail: str = "",
           stage: dict | None = None) -> bool:
    """Close a QUEUED turn CANCELLED on `rule`, record it, and owe its notices.

    CANCELLED, not FAILED: nothing went wrong, a rule decided, and a FAILED
    turn would start a debugger turn (`harness.auto_debug`). Guarded on the
    status so a turn another runner claimed in the meantime is left alone.
    """
    from apps.harness.models import Turn

    note = result_note(rule, detail)
    now = timezone.now()
    updated = Turn.objects.filter(pk=turn.pk, status=Turn.QUEUED).update(
        status=Turn.CANCELLED, finished_at=now, result_note=note,
    )
    if not updated:
        return False
    turn.refresh_from_db()
    if stage is None:
        stage = ({"runner": runner.name, "runner_kind": runner.kind} if runner is not None
                 else {"at": "enqueue"})
    _fire(turn, rule, note, stage, detail=detail)
    return True


def _fire(turn, rule: AdmissionRule, note: str, stage: dict, *, detail: str = "") -> None:
    """What every rejection records and owes, once the turn is CANCELLED."""
    from apps.harness.ledger import append_events
    from apps.harness.models import Turn

    AdmissionRule.objects.filter(pk=rule.pk).update(
        fired_count=F("fired_count") + 1, last_fired_at=timezone.now())
    append_events(turn, [{"kind": "status", "payload": {
        "status": Turn.CANCELLED, "result_note": note, "admission_rule": rule.pk, **stage,
    }}])
    _log(turn, rule, note, stage)
    turn_id, rule_id = turn.pk, rule.pk

    def _notify():
        from . import notices

        notices.deliver(turn_id, rule_id, detail=detail)

    transaction.on_commit(_notify)


def _log(turn, rule, note: str, stage: dict) -> None:
    """Best-effort line in the workspace event log: a log write must never
    fail the rejection it records."""
    try:
        from apps.events.services import record

        agent = turn.acting_agent
        record([{
            "source": "admission", "kind": REJECTED_EVENT, "level": "info",
            "summary": f"{agent.slug}: turn {turn.pk} {note}"[:500],
            "payload": {"rule_id": rule.pk, "turn_id": str(turn.pk), "agent": agent.slug, **stage},
        }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001
        logger.exception("admission: could not log the rejection of turn %s", turn.pk)
