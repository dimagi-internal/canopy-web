"""A workspace's retention rules, as its admins manage them.

Every change is written to the workspace event log with who made it, because a
rule decides what gets deleted. The log line carries the rule, never content.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.exceptions import ValidationError

from .models import RetentionRule

logger = logging.getLogger(__name__)

_KIND_NOUN = {"": "Chats and turns", RetentionRule.CHAT: "Chats", RetentionRule.TURN: "Turns",
              RetentionRule.SHARED: "Shared transcripts"}
_SOURCE_LABEL = dict(RetentionRule.SOURCE_CHOICES)
_PRINCIPAL_PHRASE = {
    RetentionRule.MEMBER: "started by members",
    RetentionRule.CONTACT: "started by contacts",
    RetentionRule.AGENT: "dispatched by another agent",
    RetentionRule.SYSTEM: "started by canopy (schedules, drills)",
}


def summary(rule: RetentionRule) -> str:
    """One readable line: "Chats from Email started by contacts with hal: 30 days"."""
    parts = [_KIND_NOUN.get(rule.kind, rule.kind)]
    if rule.source:
        parts.append(f"from {_SOURCE_LABEL.get(rule.source, rule.source)}")
    if rule.principal:
        parts.append(_PRINCIPAL_PHRASE.get(rule.principal, rule.principal))
    if rule.agent_id:
        parts.append(f"with {rule.agent.slug}")
    keep = "kept forever" if rule.keep_days is None else (
        "1 day" if rule.keep_days == 1 else f"{rule.keep_days} days")
    return f"{' '.join(parts)}: {keep}"


def rule_out(rule: RetentionRule) -> dict:
    return {
        "id": rule.pk,
        "workspace": rule.workspace_id or "",
        "kind": rule.kind,
        "source": rule.source,
        "principal": rule.principal,
        "agent": rule.agent.slug if rule.agent_id else "",
        "keep_days": rule.keep_days,
        "note": rule.note,
        "summary": summary(rule),
        "created_by": rule.created_by.email if rule.created_by_id else "",
        "updated_at": rule.updated_at,
    }


def choices() -> dict:
    def pairs(items):
        return [{"value": v, "label": label} for v, label in items]

    return {
        "kinds": pairs([(k, label) for k, label in RetentionRule.KIND_CHOICES if k != RetentionRule.SHARED]),
        "sources": pairs(RetentionRule.SOURCE_CHOICES),
        "principals": pairs(RetentionRule.PRINCIPAL_CHOICES),
    }


def policy_for(workspace, *, can_manage: bool) -> dict:
    """This workspace's own rules and the ones it inherits, nearest first."""
    rules = RetentionRule.objects.select_related("agent", "created_by")
    own = [rule_out(r) for r in rules.filter(workspace=workspace)]
    inherited = []
    for slug in [*workspace.ancestor_slugs(), None]:
        inherited += [rule_out(r) for r in rules.filter(workspace_id=slug)]
    return {
        "workspace": workspace.slug,
        "enforced": bool(getattr(settings, "CANOPY_RETENTION_ENFORCE", False)),
        "can_manage": can_manage,
        "rules": own,
        "inherited": inherited,
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


def _save(rule: RetentionRule) -> RetentionRule:
    try:
        rule.save()
    except ValidationError as exc:
        raise ValueError("; ".join(exc.messages)) from exc
    return rule


def create_rule(workspace, data: dict, *, user) -> RetentionRule:
    rule = RetentionRule(
        workspace=workspace, kind=data["kind"], source=data["source"],
        principal=data["principal"], agent=_agent(workspace, data["agent"]),
        keep_days=data["keep_days"], note=data["note"], created_by=user,
    )
    _save(rule)
    _log(workspace, user, "retention.rule_created", rule)
    return rule


def update_rule(workspace, rule_id: int, data: dict, *, user) -> RetentionRule:
    rule = RetentionRule.objects.filter(pk=rule_id, workspace=workspace).first()
    if rule is None:
        raise LookupError(rule_id)
    before = summary(rule)
    rule.kind, rule.source, rule.principal = data["kind"], data["source"], data["principal"]
    rule.agent = _agent(workspace, data["agent"])
    rule.keep_days, rule.note = data["keep_days"], data["note"]
    _save(rule)
    _log(workspace, user, "retention.rule_updated", rule, before=before)
    return rule


def delete_rule(workspace, rule_id: int, *, user) -> None:
    rule = RetentionRule.objects.filter(pk=rule_id, workspace=workspace).select_related("agent").first()
    if rule is None:
        raise LookupError(rule_id)
    _log(workspace, user, "retention.rule_deleted", rule)
    rule.delete()


def _log(workspace, user, kind: str, rule: RetentionRule, *, before: str = "") -> None:
    """Best-effort: a log write must never fail the change it records."""
    verb = {"retention.rule_created": "added", "retention.rule_updated": "changed",
            "retention.rule_deleted": "removed"}[kind]
    text = f"{user.email} {verb} a retention rule: {summary(rule)}"
    if before:
        text += f" (was: {before})"
    try:
        from apps.events.services import record

        record([{
            "source": "retention", "kind": kind, "level": "info", "summary": text[:500],
            "payload": {"rule_id": rule.pk, "user_id": user.pk, "rule": summary(rule), "before": before},
        }], workspace=workspace)
    except Exception:  # noqa: BLE001
        logger.exception("retention: could not record %s for rule %s", kind, rule.pk)
