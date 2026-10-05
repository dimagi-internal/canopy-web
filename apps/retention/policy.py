"""Which `RetentionRule` governs an item, and how an item is described to it.

Pure apart from loading the rules and walking the workspace tree once: the
sweep asks for thousands of items, so `Policy` caches both.

The rule, from the spec:
  1. nearest workspace first (own → parent → … → deployment-wide); the first
     level with ANY matching rule decides;
  2. within it, the most specific rule (most non-blank filters) wins;
  3. ties go to the shorter retention, because privacy should win a tie.
No match means keep forever.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import RetentionRule

# Chat sessions whose creator stamped a product marker. The same marker
# `canopy_sessions.services.default_origin` routes on.
_SESSION_SOURCES = {"ace-web": "ace_web", "email": "email", "slack": "slack"}
# Thread keys the channel front doors write into Session.metadata. Spelled here
# rather than imported (slack.services pulls in the whole Slack client);
# tests/test_retention.py pins them to their definitions.
SLACK_THREAD_KEY = "slack_thread"
EMAIL_THREAD_KEY = "email_thread_key"

_PRINCIPAL_FOR_INITIATOR = {
    "user": RetentionRule.MEMBER,
    "contact": RetentionRule.CONTACT,
    "agent": RetentionRule.AGENT,
}


@dataclass(frozen=True)
class Subject:
    """An item, in the vocabulary rules filter on."""

    kind: str
    workspace: str | None
    source: str = ""
    principal: str = ""
    agent_id: int | None = None


def turn_principal(initiator_kind: str) -> str:
    """`system`, `unknown` and blank all mean nobody a rule could single out."""
    return _PRINCIPAL_FOR_INITIATOR.get(initiator_kind or "", RetentionRule.SYSTEM)


def chat_principal(*, contact_id, created_by_id) -> str:
    if contact_id:
        return RetentionRule.CONTACT
    if created_by_id:
        return RetentionRule.MEMBER
    return RetentionRule.SYSTEM


def chat_source(*, metadata: dict | None, origin: str) -> str:
    metadata = metadata or {}
    marked = _SESSION_SOURCES.get(metadata.get("source") or "")
    if marked:
        return marked
    if SLACK_THREAD_KEY in metadata:
        return "slack"
    if EMAIL_THREAD_KEY in metadata:
        return "email"
    if origin == "runner":
        return RetentionRule.EMDASH
    return "canopy_web_chat"


def _matches(rule: RetentionRule, subject: Subject) -> bool:
    return (
        (not rule.kind or rule.kind == subject.kind)
        and (not rule.source or rule.source == subject.source)
        and (not rule.principal or rule.principal == subject.principal)
        and (not rule.agent_id or rule.agent_id == subject.agent_id)
    )


def _rank(rule: RetentionRule) -> tuple:
    keep = math.inf if rule.keep_days is None else rule.keep_days
    return (-rule.specificity, keep, rule.pk or 0)


class Policy:
    def __init__(self, rules: list[RetentionRule]):
        self.rules = rules
        self._by_scope: dict[str | None, list[RetentionRule]] = {}
        for rule in rules:
            self._by_scope.setdefault(rule.workspace_id, []).append(rule)
        self._chains: dict[str | None, tuple[str | None, ...]] = {}
        self._resolved: dict[Subject, RetentionRule | None] = {}

    @classmethod
    def load(cls) -> Policy:
        return cls(list(RetentionRule.objects.select_related("agent")))

    @property
    def shortest_keep_days(self) -> int | None:
        """Nothing younger than this can have expired under any rule."""
        days = [r.keep_days for r in self.rules if r.keep_days is not None]
        return min(days) if days else None

    def chain(self, workspace: str | None) -> tuple[str | None, ...]:
        if workspace not in self._chains:
            if workspace is None:
                self._chains[workspace] = (None,)
            else:
                from apps.workspaces.models import Workspace

                ws = Workspace.objects.filter(slug=workspace).first()
                ancestors = ws.ancestor_slugs() if ws else []
                self._chains[workspace] = (workspace, *ancestors, None)
        return self._chains[workspace]

    def resolve(self, subject: Subject) -> RetentionRule | None:
        if subject not in self._resolved:
            self._resolved[subject] = self._resolve(subject)
        return self._resolved[subject]

    def _resolve(self, subject: Subject) -> RetentionRule | None:
        for scope in self.chain(subject.workspace):
            matching = [r for r in self._by_scope.get(scope, []) if _matches(r, subject)]
            if matching:
                return min(matching, key=_rank)
        return None
