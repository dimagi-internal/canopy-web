"""A workspace's admission rules, as its admins manage them.

Every change is written to the workspace event log with who made it, because a
rule decides which turns never run.
"""
from __future__ import annotations

import logging

from django.core.exceptions import ValidationError

from .models import AdmissionRule

logger = logging.getLogger(__name__)

_ORIGIN = dict(AdmissionRule.ORIGIN_CHOICES)
_PRINCIPAL = {
    AdmissionRule.MEMBER: "from a member",
    AdmissionRule.CONTACT: "from a contact",
    AdmissionRule.AGENT: "from another agent",
    AdmissionRule.SYSTEM: "from canopy itself",
}
_RUNNER = dict(AdmissionRule.RUNNER_KIND_CHOICES)

#: The fields a create or update writes, beside `agent`.
_FIELDS = ("origin", "principal", "access", "capability", "turn_mode", "runner_kind",
           "action", "reason", "notify_sender", "notify_owner", "enabled")


def summary(rule: AdmissionRule) -> str:
    """One readable line: "Email from a contact, confined, manual, on a Cloud runner"."""
    parts = [f"{_ORIGIN.get(rule.origin, rule.origin)}" if rule.origin else "Any turn"]
    if rule.principal:
        parts.append(_PRINCIPAL.get(rule.principal, rule.principal))
    if rule.agent_id:
        parts.append(f"to {rule.agent.slug}")
    extra = []
    if rule.access:
        extra.append(rule.access)
    if rule.capability:
        extra.append(f"capability {rule.capability}")
    if rule.turn_mode:
        extra.append(rule.turn_mode)
    if rule.runner_kind:
        extra.append(f"on a {_RUNNER.get(rule.runner_kind, rule.runner_kind)} runner")
    text = " ".join(parts)
    return f"{text}, {', '.join(extra)}" if extra else text


def rule_out(rule: AdmissionRule) -> dict:
    return {
        "id": rule.pk,
        "workspace": rule.workspace_id,
        "agent": rule.agent.slug if rule.agent_id else "",
        **{f: getattr(rule, f) for f in _FIELDS},
        "summary": summary(rule),
        "seeded": bool(rule.seed_key),
        "fired_count": rule.fired_count,
        "last_fired_at": rule.last_fired_at,
        "created_by": rule.created_by.email if rule.created_by_id else "",
        "updated_at": rule.updated_at,
    }


def choices() -> dict:
    def pairs(items):
        return [{"value": v, "label": label} for v, label in items]

    return {
        "origins": pairs(AdmissionRule.ORIGIN_CHOICES),
        "principals": pairs(AdmissionRule.PRINCIPAL_CHOICES),
        "access": pairs(AdmissionRule.ACCESS_CHOICES),
        "turn_modes": pairs(AdmissionRule.TURN_MODE_CHOICES),
        "runner_kinds": pairs(AdmissionRule.RUNNER_KIND_CHOICES),
        "actions": pairs(AdmissionRule.ACTION_CHOICES),
    }


def policy_for(workspace, *, can_manage: bool) -> dict:
    rules = AdmissionRule.objects.filter(workspace=workspace).select_related("agent", "created_by")
    return {
        "workspace": workspace.slug,
        "can_manage": can_manage,
        "rules": [rule_out(r) for r in rules],
        "choices": choices(),
    }


def _agent(workspace, slug: str):
    if not slug:
        return None
    from apps.agents.models import Agent

    agent = Agent.objects.filter(slug=slug, workspace=workspace).first()
    if agent is None:
        raise ValueError(f"no agent '{slug}' in this workspace")
    return agent


def _save(rule: AdmissionRule) -> AdmissionRule:
    try:
        rule.save()
    except ValidationError as exc:
        raise ValueError("; ".join(exc.messages)) from exc
    return rule


def create_rule(workspace, data: dict, *, user) -> AdmissionRule:
    rule = AdmissionRule(workspace=workspace, agent=_agent(workspace, data.get("agent", "")),
                         created_by=user, **{f: data[f] for f in _FIELDS if f in data})
    _save(rule)
    _log(workspace, user, "admission.rule_created", rule)
    return rule


def update_rule(workspace, rule_id: int, data: dict, *, user) -> AdmissionRule:
    """A partial update: only the fields present in `data` change."""
    rule = AdmissionRule.objects.filter(pk=rule_id, workspace=workspace).select_related("agent").first()
    if rule is None:
        raise LookupError(rule_id)
    before = summary(rule)
    if "agent" in data:
        rule.agent = _agent(workspace, data["agent"])
    for f in _FIELDS:
        if f in data:
            setattr(rule, f, data[f])
    _save(rule)
    _log(workspace, user, "admission.rule_updated", rule, before=before)
    return rule


def delete_rule(workspace, rule_id: int, *, user) -> None:
    rule = AdmissionRule.objects.filter(pk=rule_id, workspace=workspace).select_related("agent").first()
    if rule is None:
        raise LookupError(rule_id)
    _log(workspace, user, "admission.rule_deleted", rule)
    rule.delete()


def _log(workspace, user, kind: str, rule: AdmissionRule, *, before: str = "") -> None:
    """Best-effort: a log write must never fail the change it records."""
    verb = {"admission.rule_created": "added", "admission.rule_updated": "changed",
            "admission.rule_deleted": "removed"}[kind]
    state = "" if rule.enabled else " (disabled)"
    text = f"{user.email} {verb} an admission rule: {rule.action} {summary(rule)}{state}"
    if before:
        text += f" (was: {before})"
    try:
        from apps.events.services import record

        record([{
            "source": "admission", "kind": kind, "level": "info", "summary": text[:500],
            "payload": {"rule_id": rule.pk, "user_id": user.pk, "rule": summary(rule),
                        "action": rule.action, "enabled": rule.enabled, "before": before},
        }], workspace=workspace)
    except Exception:  # noqa: BLE001
        logger.exception("admission: could not record %s for rule %s", kind, rule.pk)
