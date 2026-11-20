"""Standing rules and directives (HCP v2 6.3, 6.4): what the person set once, for every grant.

* A **standing rule** governs which clients may receive context. It only denies (6.3). It is
  enforced when a grant is issued (a grant never reaches a category a rule denies its operator)
  and on every request, so a rule set after a grant fails that grant's next read; where it
  covers every category a grant reaches, the grant is revoked (6.3, 6.2.2).
* A **directive** says what a client may do with what it received (6.4). It is delivered with every
  disclosure it applies to, whether or not the client claims to follow it, and it only narrows.

Both are the person's own act: a governance operation, over HTTP, never an agent, a token or a
site. Nothing is cached across requests: the rows are read as they stand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.db import transaction

from ..models import Directive, Person, PersonFact, PersonGrant, StandingRule
from .core import REGISTERED_CATEGORIES, Actor, audit, categories_of, denied, entry_urn, malformed

#: canopy's own agents' client operator (3.2.8): canopy itself.
PROVIDER_OPERATOR = "canopy.dimagi.com"
#: 18.1: the claim to follow every directive delivered (a client declares it at registration).
CLAIM_DIRECTIVES = "https://hcp.example/spec/clients/directives/2.0"
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
ACTIONS_KNOWN = ("share", "sign", "transact")


# --- who a client's operator is ----------------------------------------------------------


def host_of(identifier: str) -> str:
    """The host of an operator identifier (a URL, or a bare domain)."""
    ident = (identifier or "").strip().lower()
    return (urlsplit(ident).hostname or "") if "://" in ident else ident.split("/")[0]


def matches(rule_operator: str, operator_id: str) -> bool:
    """6.3: a DNS domain matches every operator whose identifier's host is that domain or a
    subdomain of it; any other value matches one operator identifier exactly."""
    rule_operator = (rule_operator or "").strip().lower()
    if _DOMAIN.match(rule_operator):
        host = host_of(operator_id)
        return host == rule_operator or host.endswith("." + rule_operator)
    return rule_operator == (operator_id or "").strip().lower()


def operator_of(grant: PersonGrant) -> tuple[str, set[str]]:
    """(the grant's client operator identifier, the conformance claims its client declared)."""
    client = grant.hcp_client if grant.hcp_client_id else None
    if client is None:
        return PROVIDER_OPERATOR, set()
    ident = client.operator_id or (host_of((client.redirect_uris or [""])[0]))
    return ident, set(client.claims or [])


# --- standing rules ------------------------------------------------------------------------


@dataclass
class Denial:
    """The categories a person's standing rules deny one client. `everything` = every category."""

    everything: bool = False
    categories: set[str] = field(default_factory=set)
    rules: list[str] = field(default_factory=list)

    def covers(self, categories) -> bool:
        return self.everything or bool(self.categories & set(categories))

    def removes(self, category: str) -> bool:
        return self.everything or category in self.categories


def denial(person: Person, operator_id: str, claims: set[str]) -> Denial:
    out = Denial()
    for r in StandingRule.objects.filter(person=person):
        if r.operators and not any(matches(o, operator_id) for o in r.operators):
            continue
        if r.requires and set(r.requires) <= claims:
            continue                      # it makes every claim the rule asks for
        out.rules.append(entry_urn(r.rule_id))
        if r.categories:
            out.categories |= set(r.categories)
        else:
            out.everything = True
    return out


def denial_for(grant: PersonGrant | None) -> Denial:
    """The denial that applies to the client holding this grant, read once per grant object (a
    request's object), so the per-entry paths cost nothing. Never across requests."""
    if grant is None:
        return Denial()
    cached = getattr(grant, "_standing_denial", None)
    if cached is None:
        op, claims = operator_of(grant)
        cached = grant._standing_denial = denial(grant.person, op, claims)
    return cached


def permitted(grant: PersonGrant | None, categories) -> list[str]:
    """Of these categories, the ones no standing rule denies this grant's client."""
    d = denial_for(grant)
    return [c for c in categories if not d.removes(c)]


def withholds(grant: PersonGrant | None, fact: PersonFact) -> bool:
    """6.3: a rule that denies any category of an entry withholds it from that operator's clients,
    whichever category a request names (4.5)."""
    return denial_for(grant).covers(categories_of(fact))


def exclusions(person: Person, operator_id: str, claims: set[str], categories) -> tuple[list[str], list[str]]:
    """(kept, excluded) of the categories a grant would reach: what 6.3 lets a grant carry, and what
    the person is told a rule excludes before they approve (6.1.6)."""
    d = denial(person, operator_id, claims)
    return ([c for c in categories if not d.removes(c)], [c for c in categories if d.removes(c)])


def _check_names(operators, requires, categories):
    def strings(value, what):
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise malformed(f"{what} is a list of non-empty strings")
        return [v.strip() for v in value]
    return strings(operators, "operators"), strings(requires, "requires"), strings(categories, "categories")


def _check_categories(categories: list[str]) -> list[str]:
    for c in categories:
        if c not in REGISTERED_CATEGORIES and not c.startswith("hcp-custom:") and not c.startswith("https://"):
            raise malformed(f"{c!r} is not a category; one of {list(REGISTERED_CATEGORIES)} or a custom category")
    return categories


@transaction.atomic
def set_rule(person: Person, *, operators=None, requires=None, categories=None, actor: Actor) -> StandingRule:
    """6.3, 9.4.14: the person sets a deny rule. It applies to existing grants from now, by the status
    check every request makes (`grants.enforce_rules`); `hcp.set_standing_rule` also revokes, at once,
    each grant it covers entirely."""
    if not actor.is_person:
        raise denied("only the person may do this")
    operators, requires, categories = _check_names(operators, requires, categories)
    if not operators and not requires:
        raise malformed("a rule names operators, or requires (every operator is allowed only with requires)")
    rule = StandingRule.objects.create(person=person, operators=operators, requires=requires,
                                       categories=_check_categories(categories))
    audit(person, "rule.set", actor=actor, detail=_rule_detail(rule))
    return rule


@transaction.atomic
def remove_rule(person: Person, rule_id, *, actor: Actor) -> None:
    if not actor.is_person:
        raise denied("only the person may do this")
    rule = StandingRule.objects.filter(person=person, rule_id=rule_id).first()
    if rule is None:
        raise denied()
    detail = _rule_detail(rule)
    rule.delete()
    audit(person, "rule.removed", actor=actor, detail=detail)


def _rule_detail(rule: StandingRule) -> str:
    return (f"ruleId={entry_urn(rule.rule_id)}; operators={rule.operators}; requires={rule.requires}; "
            f"categories={rule.categories or 'all'}; setBy={rule.set_by}")


def rule_dict(rule: StandingRule) -> dict:
    out = {"ruleId": entry_urn(rule.rule_id), "effect": "deny", "setBy": rule.set_by,
           "createdAt": rule.created_at.isoformat()}
    if rule.operators:
        out["operators"] = list(rule.operators)
    if rule.requires:
        out["requires"] = list(rule.requires)
    if rule.categories:
        out["categories"] = list(rule.categories)
    return out


# --- directives ------------------------------------------------------------------------------


@transaction.atomic
def set_directive(person: Person, *, action: str, effect: str, text: str, categories=None, operators=None,
                  actor: Actor) -> Directive:
    """6.4, 9.4.15. The person's words are `text`; every client the directive applies to receives it."""
    if not actor.is_person:
        raise denied("only the person may do this")
    action = (action or "").strip()
    if not action or not (action in ACTIONS_KNOWN or action.startswith("https://")):
        raise malformed("action is share, sign, transact or an absolute URI")
    if effect not in (Directive.DENY, Directive.ASK_FIRST, Directive.PERMIT):
        raise malformed("effect is deny, ask-first or permit")
    text = " ".join((text or "").split())
    if not text:
        raise malformed("a directive needs its text, in your words")
    operators, _, categories = _check_names(operators, None, categories)
    d = Directive.objects.create(person=person, action=action, effect=effect, text=text[:1000],
                                 categories=_check_categories(categories), operators=operators)
    audit(person, "directive.set", actor=actor, detail=_directive_detail(d))
    return d


@transaction.atomic
def remove_directive(person: Person, directive_id, *, actor: Actor) -> None:
    if not actor.is_person:
        raise denied("only the person may do this")
    d = Directive.objects.filter(person=person, directive_id=directive_id).first()
    if d is None:
        raise denied()
    detail = _directive_detail(d)
    d.delete()
    audit(person, "directive.removed", actor=actor, detail=detail)


def _directive_detail(d: Directive) -> str:
    return (f"directiveId={entry_urn(d.directive_id)}; action={d.action}; effect={d.effect}; "
            f"categories={d.categories or 'all'}; operators={d.operators or 'all'}; setBy={d.set_by}")


def directive_dict(d: Directive) -> dict:
    out = {"directiveId": entry_urn(d.directive_id), "action": d.action, "effect": d.effect, "text": d.text,
           "setBy": d.set_by, "createdAt": d.created_at.isoformat()}
    if d.categories:
        out["categories"] = list(d.categories)
    if d.operators:
        out["operators"] = list(d.operators)
    return out


def directives_for(grant: PersonGrant | None, facts=None) -> list[Directive]:
    """6.4 delivery: the directives whose operators match this client's operator (or are absent) and
    whose categories include one of the entries disclosed (or are absent). A client with no entries
    disclosed (reading its own grant) gets those that apply to it."""
    if grant is None:
        return []
    op, _ = operator_of(grant)
    disclosed = {c for f in (facts or []) for c in categories_of(f)}
    out = []
    for d in Directive.objects.filter(person=grant.person):
        if d.operators and not any(matches(o, op) for o in d.operators):
            continue
        if d.categories and facts is not None and not (set(d.categories) & disclosed):
            continue
        out.append(d)
    return out


def delivered(grant: PersonGrant | None, facts=None) -> list[dict]:
    return [directive_dict(d) for d in directives_for(grant, facts)]
