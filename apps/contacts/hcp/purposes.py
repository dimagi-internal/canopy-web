"""Purposes: the reason an agent gives for reading what canopy remembers about a person
(HCP v2 6.1.5.2, 4.2.6, 7.1).

Each purpose is a URI plus a sentence the person can understand. The draft defines no shared
list, so canopy keeps its own. They are recorded in three places:

* **a grant** — `PersonGrant.purposes`, shown to the person before the act that makes the
  grant (6.1.6); a read naming a purpose the grant does not carry is refused;
* **an entry's writer** — `PersonFact.permitted_purposes` (4.2.6: the uses its asserter
  permitted travel with it);
* **the person** — `PersonFact.restricted_purposes`, a governance narrowing
  (`restrict_purposes`), which can only narrow.

A purpose binds the client's *declaration*; canopy cannot see what a caller does with what it
reads (6.1.5.2). The v1 wire has free-text `purpose` only, so the adapter keeps that text as
the audit's description and supplies the grant's own default code (`read_purpose`).
"""
from __future__ import annotations

from django.db import transaction

from ..models import PersonFact
from .core import Actor, audit, denied, malformed

BASE = "https://canopy.dimagi.com/hcp/purposes/"
ASSISTANCE = BASE + "agent-assistance"
MEMORY_KEEPING = BASE + "memory-keeping"
COACHING = BASE + "coaching"

#: Written for the person who reads it on the consent screen and in the session prompt.
PURPOSES = {
    ASSISTANCE: "An agent you are working with uses what it knows about you to help with the task in front of it",
    MEMORY_KEEPING: "An agent remembers what you tell it about yourself and your work, for later",
    COACHING: "Coaches use what canopy has learned about how you work to suggest better ways to work",
}
#: Which purpose each action of a grant serves (read → assistance, write → memory keeping).
FOR_ACTION = {"read": ASSISTANCE, "write": MEMORY_KEEPING}


def known(code: str) -> bool:
    return code in PURPOSES


def describe(codes) -> list[dict]:
    """`[{code, description}]`, as a grant lists them and a prompt shows them."""
    return [{"code": c, "description": PURPOSES.get(c, c)} for c in codes or []]


def check_codes(codes) -> list[str]:
    """A non-empty list of registered purpose codes, without duplicates (a grant is never
    issued without one, 6.1.5.2)."""
    if not isinstance(codes, (list, tuple)) or not codes:
        raise malformed("purposes is a non-empty list of purpose codes")
    out = []
    for c in codes:
        if not isinstance(c, str) or not known(c):
            raise malformed(f"{c!r} is not a purpose this instance knows; one of {sorted(PURPOSES)}")
        if c not in out:
            out.append(c)
    return out


def for_scopes(scopes) -> list[str]:
    """The purposes of what a set of scopes allows: read → assistance, write → memory keeping."""
    actions = {s.split(":")[-1] for s in scopes or [] if s.startswith("hcp:")}
    return [FOR_ACTION[a] for a in ("read", "write") if a in actions]


def of_grant(grant) -> list[str]:
    """What a grant carries. A grant made before purposes existed stores none, and reads as
    the purposes of its scopes — exactly what it was issued for."""
    if grant is None:
        return []
    return list(grant.purposes or []) or for_scopes(grant.scopes)


def read_purpose(grant, requested: str | None) -> str:
    """The purpose code a read is made under. A caller that names one (v2) must name one the
    grant carries; one that names none (every v1 caller) is given the grant's default — its read
    purpose when it has one."""
    carried = of_grant(grant)
    if requested:
        if not requested.startswith("https://"):
            raise malformed("purposeCode is a purpose code (an absolute URI)")
        if requested not in carried:
            raise denied(f"not authorized for purpose {requested}")
        return requested
    for c in carried:
        if c != MEMORY_KEEPING:
            return c
    return carried[0] if carried else ""


def entry_serves(fact: PersonFact, code: str) -> bool:
    """Can this entry be handed out for `code` (4.2.6)? Its writer's list and the person's narrowing
    each limit it when set; the code has to be on every list that is set."""
    for limit in (fact.permitted_purposes, fact.restricted_purposes):
        if limit is not None and code not in limit:
            return False
    return True


def check_permitted(codes) -> list[str] | None:
    """What a writing client may say it permits: None (no limit) or registered codes."""
    if codes is None:
        return None
    return check_codes(codes)


@transaction.atomic
def restrict_purposes(fact: PersonFact, codes, *, actor: Actor) -> PersonFact:
    """The Authorizing Principal narrows an entry's purposes (4.2.6, a governance operation:
    the person, never an agent). `None` removes the narrowing; it never widens past what the
    entry's writer permitted, and a purpose the writer did not permit is refused rather than
    silently ineffective. Recorded as `context.restricted`, naming the lists before and after."""
    if not actor.is_person:
        raise denied("only the person may do this")
    if fact.superseded_at is not None:
        raise denied()
    codes = None if codes is None else check_codes(codes)
    if codes is not None and fact.permitted_purposes is not None:
        beyond = [c for c in codes if c not in fact.permitted_purposes]
        if beyond:
            raise malformed(f"{beyond} are not among the purposes the entry's writer permitted")
    before = fact.restricted_purposes
    PersonFact.objects.filter(pk=fact.pk).update(restricted_purposes=codes)
    fact.restricted_purposes = codes
    audit(fact.person, "context.restricted", actor=actor, entry=fact,
          detail=f"from={before!r}; to={codes!r}")
    return fact
