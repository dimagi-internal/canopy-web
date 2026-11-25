"""Relationships between entries (HCP v2 4.2.3): `derivedFrom`, `supersedes`, `contradicts`, `duplicates`.

Asserted by a client with a write, by the person, or by the provider's Memory Manager (15). The
provider records who and when; neither is taken from a request. `derivedFrom` may name a SOURCE
(a session, later a meeting or a mail thread) as well as another entry, by a loose `(kind, ref)`
pair, so an entry learned from a session can say so without this app depending on the brain's.
"""
from __future__ import annotations

import re

from ..models import EntryRelationship, Person, PersonFact
from . import rules
from .core import Actor, denied, entry_urn, malformed, parse_entry_id, scope

PROVIDER = "canopy"
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SOURCE_URN = "urn:canopy:source:"


def role_of(actor: Actor) -> str:
    """3.9: the person acts through a management client, anyone else through a context client."""
    return EntryRelationship.MANAGEMENT_CLIENT if actor.is_person else EntryRelationship.CONTEXT_CLIENT


def _record(person: Person, entry_id, type_: str, *, role: str, by: str, target_entry_id=None,
            source: tuple[str, str] | None = None, proposal: dict | None = None) -> EntryRelationship:
    row, _ = EntryRelationship.objects.get_or_create(
        person=person, entry_id=entry_id, type=type_, target_entry_id=target_entry_id,
        source_kind=source[0] if source else "", source_ref=source[1] if source else "",
        defaults={"asserted_by_role": role, "asserted_by_id": by[:200], "proposal": proposal})
    return row


def record_contradiction(a: PersonFact, b: PersonFact, *, role: str = EntryRelationship.HCP_PROVIDER,
                         by: str = PROVIDER) -> None:
    """A `contradicts` relationship is symmetric: the provider records it on both entries (4.2.3)."""
    _record(a.person, a.entry_id, EntryRelationship.CONTRADICTS, role=role, by=by, target_entry_id=b.entry_id)
    _record(a.person, b.entry_id, EntryRelationship.CONTRADICTS, role=role, by=by, target_entry_id=a.entry_id)


def remove_provider_contradiction(a_entry_id, b_entry_id, person: Person) -> None:
    EntryRelationship.objects.filter(
        person=person, type=EntryRelationship.CONTRADICTS, asserted_by_role=EntryRelationship.HCP_PROVIDER,
        entry_id__in=[a_entry_id, b_entry_id], target_entry_id__in=[a_entry_id, b_entry_id]).delete()


def parse_specs(specs) -> list[dict]:
    """The client's `derivedFrom` list: each `{entryId}` or `{source: {kind, ref}}`."""
    if specs is None:
        return []
    if not isinstance(specs, list) or len(specs) > 20:
        raise malformed("derivedFrom is a list of at most 20 entries or sources")
    out = []
    for s in specs:
        if not isinstance(s, dict):
            raise malformed("each derivedFrom names an entryId or a source {kind, ref}")
        src = s.get("source")
        if src is not None:
            kind, ref = str((src or {}).get("kind") or ""), str((src or {}).get("ref") or "")
            if not _KIND.match(kind) or not ref or len(ref) > 200:
                raise malformed("a source is {kind: a short lowercase word, ref: its id}")
            out.append({"source": (kind, ref)})
        elif s.get("entryId"):
            out.append({"entry": parse_entry_id(str(s["entryId"]))})
        else:
            raise malformed("each derivedFrom names an entryId or a source {kind, ref}")
    return out


def assert_derived_from(fact: PersonFact, specs: list[dict], *, actor: Actor, grant=None) -> None:
    """Record `derivedFrom` for a write. A client may name only an entry in a category its grant
    reads, and an unreadable or missing one fails the same way, disclosing nothing (4.2.3)."""
    for s in specs:
        if "source" in s:
            kind, ref = s["source"]
            _record(fact.person, fact.entry_id, EntryRelationship.DERIVED_FROM, role=role_of(actor),
                    by=actor.id, source=(kind, ref))
            continue
        target = PersonFact.objects.filter(person=fact.person, entry_id=s["entry"],
                                           superseded_at__isnull=True).first()
        if target is None or target.entry_id == fact.entry_id:
            raise denied()
        if not actor.is_person and not _reads(grant, target.category):
            raise denied()
        _record(fact.person, fact.entry_id, EntryRelationship.DERIVED_FROM, role=role_of(actor),
                by=actor.id, target_entry_id=target.entry_id)

def _reads(grant, category: str) -> bool:
    """Does the grant read this category, no standing rule denying it (the check `grants.allows` makes)?"""
    return (grant is not None and scope(category, "read") in (grant.scopes or [])
            and not rules.denial_for(grant).removes(category))


def render(fact: PersonFact) -> list[dict]:
    out = []
    for r in EntryRelationship.objects.filter(person_id=fact.person_id, entry_id=fact.entry_id):
        target = entry_urn(r.target_entry_id) if r.target_entry_id else f"{SOURCE_URN}{r.source_kind}:{r.source_ref}"
        rel = {"type": r.type, "target": target,
               "assertedBy": {"role": r.asserted_by_role, "id": r.asserted_by_id},
               "recordedAt": r.recorded_at.isoformat()}
        if r.proposal:
            rel["proposal"] = dict(r.proposal)
        out.append(rel)
    return out
