"""Entries (the `PersonFact` chain), their wire shape, conflicts (2.6) and writes."""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import EntryRelationship, Person, PersonAuditEvent, PersonFact
from . import confirmations, relationships
from .consent import session_of
from .core import (
    BASIS_OF,
    CATEGORY_KIND,
    CONTEXT,
    DECLARATION,
    HELD_CATEGORIES,
    Actor,
    audit,
    captured_by_model,
    denied,
    entry_urn,
    malformed,
    normalize_dimension,
    valid_category,
)
from .purposes import check_permitted
from .sensitive import SensitiveRefused

# --- entries -------------------------------------------------------------------


def current_versions(person: Person, workspace_slug: str | None = None):
    """Every entry's latest version, whatever its status (the person's view)."""
    qs = PersonFact.objects.filter(person=person, superseded_at__isnull=True)
    if workspace_slug is not None:
        qs = qs.filter(workspace_id=workspace_slug)
    return qs


def servable(qs, *, now: dt.datetime | None = None):
    """What may reach an AGENT: active (so never conflicted, deprecated or
    deleted — 2.6.2), current, and not past its expirationDate."""
    now = now or timezone.now()
    return (qs.filter(superseded_at__isnull=True, retracted_at__isnull=True,
                      status=PersonFact.ACTIVE)
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now)))


def current(entry_id: uuid.UUID) -> PersonFact | None:
    return (PersonFact.objects.select_related("person", "workspace", "asserted_by_agent",
                                              "asserted_by_user", "source_turn")
            .filter(entry_id=entry_id, superseded_at__isnull=True).first())


def version_of(entry_id: uuid.UUID, version: int) -> PersonFact | None:
    return PersonFact.objects.filter(entry_id=entry_id, version=version).first()


def provenance_source(fact: PersonFact) -> str:
    if fact.provenance_source:
        return fact.provenance_source
    if fact.source_turn_id:
        return f"turn:{fact.source_turn_id}"
    if fact.source_contact_id:
        return f"integration:contact-notes:{fact.source_contact_id}"
    return "user-input"


def captured_by(fact: PersonFact) -> str:
    if fact.captured_by:
        return fact.captured_by
    if fact.asserted_by_agent_id:
        return f"agent:{fact.asserted_by_agent.slug}"
    if fact.asserted_by_user_id:
        return f"user:{fact.asserted_by_user.email}"
    return "canopy"


def subject_id(person: Person) -> str:
    """`claim.subject.id` — a pseudonym, never an address (2.3 uses one)."""
    return f"did:hcp:user:canopy-person-{person.pk}"


def site_of(turn) -> dict:
    """The host site a turn's session came through — its server-owned
    `embed_app` (from the token that created it, never the page) and the page it
    was on — or {} for a conversation in canopy itself."""
    session = session_of(turn) if turn is not None else None
    name = ((session.metadata or {}).get("embed_app") or "") if session is not None else ""
    if not name:
        return {}
    page = str((session.page_state or {}).get("path") or "")[:300]
    return {"name": str(name)[:100], **({"page": page} if page else {})}


def to_entry(fact: PersonFact, *, redact: bool) -> dict:
    """The grouped Common Envelope (2.2). `redact` drops the 4.4.4 fields.
    `provenance.site` is canopy's extension (the site an agent was reached
    through); it never affects scope, and is redacted with the 4.4.4 fields."""
    provenance = {"source": provenance_source(fact), "capturedAt": _iso(fact.created_at),
                  "capturedBy": captured_by(fact), "userVerified": confirmations.user_verified(fact)}
    if fact.provenance_site:
        provenance["site"] = dict(fact.provenance_site)
    if redact:
        provenance.pop("source")
        provenance.pop("capturedBy")
        provenance.pop("site", None)
    record = {
        "category": fact.category,
        "dimension": fact.dimension or None,
        "declarationType": DECLARATION[fact.basis],
        "confidence": fact.confidence or None,
        "status": fact.status,
        "version": fact.version,
        "previousVersion": fact.version - 1 if fact.version > 1 else None,
        "provenance": provenance,
        "metadata": _metadata(fact),
    }
    return {
        "@context": CONTEXT,
        "id": entry_urn(fact.entry_id),
        "type": ["HCPEntry"],
        "credentialType": "NLPreference",
        "claim": {
            "issuer": {"id": (f"https://canopy.dimagi.com/w/{fact.workspace_id}" if fact.workspace_id
                              else "https://canopy.dimagi.com/people/me"), "type": "HCPSource"},
            "issuanceDate": _iso(fact.created_at),
            "expirationDate": _iso(fact.expires_at),
            "subject": {"id": subject_id(fact.person), "relationship": fact.relationship or None,
                        "preference": fact.statement},
        },
        "record": record,
    }


def to_minimal(fact: PersonFact) -> dict:
    """3.1.1 `responseDetail: minimal`."""
    return {"id": entry_urn(fact.entry_id), "record": {"category": fact.category,
                                                       "dimension": fact.dimension or None},
            "value": fact.statement}


def _metadata(fact: PersonFact) -> dict:
    """canopy's own bookkeeping, in `record.metadata` (2.2: never affects scope)."""
    meta = dict(fact.metadata or {})
    meta["canopy:kind"] = fact.kind
    meta["canopy:workspace"] = fact.workspace_id      # None: a personal entry
    if fact.instance_ref:
        meta["canopy:instance"] = fact.instance_ref
    if fact.project_id and not fact.project.is_personal:
        meta["canopy:project"] = fact.project.ext_id or str(fact.project_id)
    return meta


def _iso(value) -> str | None:
    return value.isoformat() if value else None


# --- conflicts (v2 4.7, Memory Manager 15) ----------------------------------------------
#
# Two entries conflict when a `contradicts` relationship links them. The method that finds one is
# declared in discovery as `conflict_detection` (`CONFLICT_DETECTION`): structural, because canopy-web
# makes no model calls of its own. It compares an active model-inferred entry with the person's other
# active entries of the same person, workspace and category that name the SAME non-empty normalized
# dimension (a hint, 15.2) and say something different.
#
# Which rule decides what a conflict changes depends on the form of the write that found it:
#
# * **v2** (`hcp_v2_api`, `v2=True`): standing (4.7.1, `confirmations.is_authoritative`), never the
#   declaration type alone, so an agent's "the person said it" has none until the person confirms it,
#   and any inference may conflict with any other entry;
# * **v1** (every other caller: the v1 routes and MCP tools, `/api/people/…/facts/`, the CLI, the
#   contact-notes mirror): v1's rule, unchanged for those callers. Only a declared entry and an
#   inference conflict, and the declared side outranks the inference whoever wrote it, so only the
#   inference is held.

CONFLICT_DETECTION = "structural"


def _same_words(a: str, b: str) -> bool:
    return " ".join(a.lower().split()).rstrip(".") == " ".join(b.lower().split()).rstrip(".")


_V1_PAIR = {PersonFact.INFERRED: PersonFact.DECLARED, PersonFact.DECLARED: PersonFact.INFERRED}


def _candidates(fact: PersonFact, *, v2: bool):
    """Current, live, active entries this one could conflict with. An entry with no dimension is
    never conflicted automatically (15.2); a pair needs a model-inferred side (15.2: those are the
    pairs whose conflicts change status), and an entry that only repeats another is not a conflict.
    v1's rule pairs only a declared entry with an inference (attestations were out of its scope)."""
    if not fact.dimension:
        return []
    qs = (PersonFact.objects.filter(person_id=fact.person_id, workspace_id=fact.workspace_id,
                                    category=fact.category, dimension=fact.dimension,
                                    status=PersonFact.ACTIVE, superseded_at__isnull=True,
                                    retracted_at__isnull=True)
          .exclude(entry_id=fact.entry_id))
    if not v2:
        other = _V1_PAIR.get(fact.basis)
        if other is None:
            return []
        qs = qs.filter(basis=other)
    elif fact.basis != PersonFact.INFERRED:
        qs = qs.filter(basis=PersonFact.INFERRED)
    return [o for o in qs if not _same_words(o.statement, fact.statement)]


def has_standing(x: PersonFact, *, v2: bool) -> bool:
    """Does `x` prevail in a conflict? v2: `confirmations.is_authoritative`. v1: a declared entry
    always does, whoever wrote it, and anything else only as v2 would have it (a confirmed one)."""
    if not v2 and x.basis == PersonFact.DECLARED:
        return True
    return confirmations.is_authoritative(x)


def _authority(x: PersonFact, other: PersonFact, *, v2: bool) -> bool:
    """Is `x` authoritative against `other` (4.7.1), an attestation counting so against an inference (4.4)?"""
    if x.basis == PersonFact.ATTESTED and other.basis == PersonFact.INFERRED:
        return True
    return has_standing(x, v2=v2)


def _held(fact: PersonFact, *, actor: Actor, authoritative: PersonFact | None, other: PersonFact) -> None:
    PersonFact.objects.filter(pk=fact.pk).update(status=PersonFact.CONFLICTED)
    fact.status = PersonFact.CONFLICTED
    audit(fact.person, "conflict.detected", actor=actor, entry=fact,
          related_entry_id=(authoritative or other).entry_id,
          detail=f"dimension {fact.dimension!r}: the entry is held until the person resolves it")


def _conflict(fact: PersonFact, other: PersonFact, *, actor: Actor, v2: bool) -> list[PersonFact]:
    """Record that two entries contradict and apply 4.7.2. Returns the entries newly held."""
    relationships.record_contradiction(fact, other)
    held = []
    if PersonFact.INFERRED not in (fact.basis, other.basis):
        # Any other pair (declared, attested): surfaced to the person, no status changes (4.7).
        audit(fact.person, "conflict.detected", actor=actor, entry=fact, related_entry_id=other.entry_id,
              detail="the entries contradict; neither is a model inference, so no status changed")
        return held
    if (fact.basis == other.basis == PersonFact.INFERRED and fact.captured_by == other.captured_by
            and not confirmations.is_authoritative(fact) and not confirmations.is_authoritative(other)):
        # The same asserter corrected itself: the earlier inference is set aside, not quarantined (4.7).
        older = min((fact, other), key=lambda f: (f.created_at, f.pk))
        newer = other if older is fact else fact
        PersonFact.objects.filter(pk=older.pk).update(status=PersonFact.DEPRECATED, embedding=None,
                                                      embedding_model="")
        older.status = PersonFact.DEPRECATED
        audit(fact.person, "conflict.detected", actor=actor, entry=older, related_entry_id=newer.entry_id,
              detail=f"dimension {fact.dimension!r}: the same asserter inferred it again")
        audit(fact.person, "conflict.resolved", actor=actor, entry=older, related_entry_id=newer.entry_id,
              detail="transition=asserter-superseded")
        return held
    fa, oa = _authority(fact, other, v2=v2), _authority(other, fact, v2=v2)
    if fa and oa:
        audit(fact.person, "conflict.detected", actor=actor, entry=fact, related_entry_id=other.entry_id,
              detail=f"dimension {fact.dimension!r}: both have standing; surfaced, no status changed")
    elif fa:
        _held(other, actor=actor, authoritative=fact, other=fact)
        held.append(other)
    elif oa:
        _held(fact, actor=actor, authoritative=other, other=other)
        held.append(fact)
    else:
        for x, y in ((fact, other), (other, fact)):
            _held(x, actor=actor, authoritative=None, other=y)
            held.append(x)
    return held


def assert_relationship(fact: PersonFact, type_: str, other: PersonFact, *, actor: Actor) -> None:
    """A client's (or the person's) `duplicates` / `supersedes` / `contradicts` between two of the
    person's entries (4.2.3). `contradicts` is symmetric and runs the consequences of 4.7.2;
    `supersedes` deprecates the target where the person asserted it or the target's own asserter did."""
    if other.entry_id == fact.entry_id:
        raise malformed("an entry cannot relate to itself")
    if type_ == EntryRelationship.CONTRADICTS:
        _conflict(fact, other, actor=actor, v2=True)
        EntryRelationship.objects.filter(person=fact.person, type=type_, entry_id__in=[fact.entry_id, other.entry_id],
                                         target_entry_id__in=[fact.entry_id, other.entry_id]).update(
            asserted_by_role=relationships.role_of(actor), asserted_by_id=actor.id[:200])
    elif type_ in (EntryRelationship.DUPLICATES, EntryRelationship.SUPERSEDES):
        relationships._record(fact.person, fact.entry_id, type_, role=relationships.role_of(actor),
                              by=actor.id, target_entry_id=other.entry_id)
        if type_ == EntryRelationship.SUPERSEDES and (actor.is_person or captured_by(other) == actor.id):
            PersonFact.objects.filter(pk=other.pk).update(status=PersonFact.DEPRECATED)
    else:
        raise malformed("a relationship is derivedFrom, supersedes, contradicts or duplicates")


def detect_conflicts(fact: PersonFact, *, actor: Actor, v2: bool = False) -> list[PersonFact]:
    """Evaluate on every write, symmetrically (15.2). Returns the entries newly held. `v2`: the
    write came through the v2 binding, so standing decides; otherwise v1's rule (see above)."""
    if fact.status != PersonFact.ACTIVE:
        return []
    marked = []
    for other in _candidates(fact, v2=v2):
        if fact.status != PersonFact.ACTIVE:
            break                                   # an earlier pair already held this one
        marked += _conflict(fact, other, actor=actor, v2=v2)
    return marked


def reevaluate(fact: PersonFact, *, actor: Actor) -> None:
    """15.3: a revision may end a conflict the provider found. Each `contradicts` the provider
    asserted on this entry is kept only while the method still finds it; one that no longer holds is
    removed and any entry it held returns to active, with `conflict.resolved`."""
    rows = EntryRelationship.objects.filter(
        person_id=fact.person_id, entry_id=fact.entry_id, type=EntryRelationship.CONTRADICTS,
        asserted_by_role=EntryRelationship.HCP_PROVIDER, target_entry_id__isnull=False)
    for r in list(rows):
        other = current(r.target_entry_id)
        if other is None or other.retracted_at is not None:
            relationships.remove_provider_contradiction(fact.entry_id, r.target_entry_id, fact.person)
            continue
        same = (other.workspace_id == fact.workspace_id and other.category == fact.category
                and other.dimension and other.dimension == fact.dimension
                and not _same_words(other.statement, fact.statement)
                and PersonFact.INFERRED in (fact.basis, other.basis))
        if same:
            continue
        relationships.remove_provider_contradiction(fact.entry_id, other.entry_id, fact.person)
        for held in (fact, other):
            if held.status == PersonFact.CONFLICTED and not confirmations.still_in_conflict(held):
                PersonFact.objects.filter(pk=held.pk).update(status=PersonFact.ACTIVE)
                held.status = PersonFact.ACTIVE
                audit(fact.person, "conflict.resolved", actor=actor, entry=held,
                      related_entry_id=(other if held is fact else fact).entry_id,
                      detail="transition=superseded: the revision no longer conflicts")


# --- writes ----------------------------------------------------------------------


@transaction.atomic
def add_entry(*, person: Person, workspace, category: str, statement: str,
              declaration: str, actor: Actor, confidence: str | None = None,
              dimension: str | None = None, kind: str | None = None, source: str = "",
              source_turn=None, by_user=None, project=None, instance_ref: str = "",
              expires_at=None, relationship: str = "", metadata: dict | None = None,
              user_verified: bool = False, captured_by_override: str = "",
              permitted_purposes=None, derived_from=None, grant=None, v2: bool = False) -> PersonFact:
    """addPreference (3.2.2 / 3.3.2): a new entry at version 1, audited, and
    conflict-checked (by v2's rule when `v2`, the v2 binding's write; else v1's)."""
    from .. import people

    category = (category or "").strip()
    if not valid_category(category):
        raise malformed(f"category {category!r} is not one canopy holds; one of "
                        f"{list(HELD_CATEGORIES)} or {PersonFact.CUSTOM_PREFIX}<name>")
    basis = BASIS_OF.get(declaration)
    if basis is None:
        raise malformed("declarationType is user-declared, model-inferred or issuer-attested")
    confidence = _check_confidence(basis, confidence)
    permitted_purposes = check_permitted(permitted_purposes)
    derived_specs = relationships.parse_specs(derived_from)
    kind = kind or CATEGORY_KIND.get(category, PersonFact.PREFERENCE)
    try:
        fact = people.record_fact(
            person=person, workspace=workspace, kind=kind, statement=statement, basis=basis,
            by_user=by_user if actor.type == PersonAuditEvent.USER else None,
            by_agent=actor.agent, source_turn=source_turn, project=project,
            instance_ref=instance_ref, category=category, dimension=dimension,
            confidence=confidence, provenance_source=source, expires_at=expires_at,
            relationship=relationship, metadata=metadata, user_verified=user_verified,
            captured_by=captured_by_override or actor.id, actor=actor,
            permitted_purposes=permitted_purposes, v2_conflicts=v2)
    except SensitiveRefused as exc:
        raise denied(str(exc)) from None
    except people.FactError as exc:
        raise malformed(str(exc)) from None
    relationships.assert_derived_from(fact, derived_specs, actor=actor, grant=grant)
    return fact


def _check_confidence(basis: str, confidence: str | None) -> str:
    """2.2: confidence present and non-null iff model-inferred — else 422."""
    confidence = (confidence or "").strip().lower() if confidence is not None else ""
    if basis == PersonFact.INFERRED:
        if confidence not in PersonFact.CONFIDENCES:
            raise malformed("a model-inferred entry needs confidence: high, medium or low")
        return confidence
    if confidence:
        raise malformed("confidence is only for model-inferred entries")
    return ""


@transaction.atomic
def update_entry(fact: PersonFact, *, statement: str, reason: str, actor: Actor,
                 category: str | None = None, dimension: str | None = None,
                 by_user=None, source_turn=None, model: str | None = None, v2: bool = False) -> PersonFact:
    """updatePreference (3.2.3 / 3.3.3): a NEW VERSION of the same entry. `v2`: the v2 binding's
    revision, conflict-checked by v2's rule; else v1's.

    * the person updating an inference ACCEPTS it (2.6.3): it becomes
      user-declared and active, and the declared entry it contradicted is
      deprecated;
    * a category change moves the entry between grant scopes, so only the
      person may make it (2.2), and it is audited with both categories.
    """
    from .. import people

    if fact.retracted_at is not None or fact.superseded_at is not None:
        raise denied()
    if not actor.is_person and confirmations.latest(fact) == "rejected":
        raise denied()          # 3.8: a client may not revise what the person rejected
    new_category = (category or "").strip() or fact.category
    if new_category != fact.category:
        if not actor.is_person:
            raise denied("only the person may move an entry to another category")
        if not valid_category(new_category):
            raise malformed(f"category {new_category!r} is not one canopy holds")
    new_dimension = fact.dimension if dimension is None else normalize_dimension(dimension)
    was_conflicted = fact.status == PersonFact.CONFLICTED
    basis, confidence = fact.basis, fact.confidence
    if actor.is_person and fact.basis == PersonFact.INFERRED:
        basis, confidence = PersonFact.DECLARED, ""     # the person has now declared it
    captured = captured_by_model(actor, inferred=basis == PersonFact.INFERRED, model=model)
    try:
        new = people.record_fact(
            person=fact.person, workspace=fact.workspace, kind=fact.kind, statement=statement,
            basis=basis, by_user=by_user if actor.is_person else None, by_agent=actor.agent,
            source_turn=source_turn, project=fact.project,
            instance_ref=fact.instance_ref, supersedes=fact, category=new_category,
            dimension=new_dimension, confidence=confidence,
            provenance_source=fact.provenance_source, expires_at=fact.expires_at,
            relationship=fact.relationship, metadata=fact.metadata,
            user_verified=fact.user_verified or actor.is_person, captured_by=captured,
            reason=reason, actor=actor, status=PersonFact.ACTIVE, audit_event="preference.updated",
            audit_detail=(f"category {fact.category} -> {new_category}; " if new_category != fact.category
                          else "") + (reason or ""),
            check_conflicts=not (was_conflicted and actor.is_person),
            # What its writer permitted belongs to the version and a v1 revision does not
            # change it; the person's narrowing is classification and survives a revision (4.2.1).
            permitted_purposes=fact.permitted_purposes, restricted_purposes=fact.restricted_purposes,
            v2_conflicts=v2)
    except SensitiveRefused as exc:
        raise denied(str(exc)) from None
    except people.FactError as exc:
        raise malformed(str(exc)) from None
    if was_conflicted and not actor.is_person and new.status == PersonFact.ACTIVE:
        audit(new.person, "conflict.resolved", actor=actor, entry=new,
              detail="transition=superseded: the revision no longer conflicts")
    if was_conflicted and actor.is_person:
        # The person edited a held entry: that version is their own statement (3.8), so it has
        # standing, and the authoritative entry it contradicted is set aside (4.7.3), unless it is an
        # attestation, which never is.
        for other in confirmations._live_partners(new):
            if other.status == PersonFact.ACTIVE and other.basis != PersonFact.ATTESTED \
                    and has_standing(other, v2=v2):
                PersonFact.objects.filter(pk=other.pk).update(status=PersonFact.DEPRECATED)
                audit(new.person, "conflict.resolved", actor=actor, entry=new,
                      related_entry_id=other.entry_id,
                      detail="transition=accepted: the person's own statement outranks the one it contradicted")
    return new


@transaction.atomic
def delete_entry(fact: PersonFact, *, actor: Actor, reason: str, hard: bool = False,
                 by_user=None) -> None:
    """deletePreference (3.2.4 / 3.3.4). Soft: the entry's status becomes
    deleted (it is still held, and the person still sees it). Hard: every
    version is removed; the audit event keeps only the id and category."""
    from .. import people

    if hard and not actor.is_person:
        raise denied("only the person may hard-delete")
    person, entry_id, category = fact.person, fact.entry_id, fact.category
    was_conflicted = fact.status == PersonFact.CONFLICTED
    partners = list(confirmations._live_partners(fact))
    if hard:
        # Nothing of the entry stays, including what points at it (a relationship, a confirmation).
        EntryRelationship.objects.filter(person=person).filter(
            Q(entry_id=entry_id) | Q(target_entry_id=entry_id)).delete()
        PersonFact.objects.filter(entry_id=entry_id).delete()
        audit(person, "preference.hardDeleted", actor=actor, entry_id=entry_id, category=category,
              workspace_slug=fact.workspace_id, detail=reason)
    else:
        if fact.retracted_at is not None:
            return
        people.retract(fact, by=by_user, actor=actor, reason=reason)
    if was_conflicted:
        audit(person, "conflict.resolved", actor=actor, entry_id=entry_id, category=category,
              workspace_slug=fact.workspace_id, detail="transition=rejected: the entry was deleted")
    # What it held returns to active when nothing else it contradicted remains (4.7.3).
    confirmations.release(partners, actor=actor, because=fact)
