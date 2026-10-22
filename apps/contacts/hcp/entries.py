"""Entries (the `PersonFact` chain), their wire shape, conflicts (2.6) and writes."""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import Person, PersonAuditEvent, PersonFact
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
from .zdr import ZdrRefused

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
    """The connected site a turn's session came through — its server-owned
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
                  "capturedBy": captured_by(fact), "userVerified": bool(fact.user_verified)}
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
    if fact.project_id:
        meta["canopy:project"] = fact.project.ext_id or str(fact.project_id)
    return meta


def _iso(value) -> str | None:
    return value.isoformat() if value else None


# --- conflicts (2.6) ------------------------------------------------------------


def _candidates(fact: PersonFact):
    """Current versions this entry could conflict with: same person, workspace
    and category, the SAME non-empty normalized dimension (the mandatory case),
    the other declaration type of the inferred/declared pair. An entry with no
    dimension is never conflicted automatically (2.6.1); issuer-attested ones
    are out of scope at v1."""
    if not fact.dimension:
        return PersonFact.objects.none()
    other = {PersonFact.INFERRED: PersonFact.DECLARED,
             PersonFact.DECLARED: PersonFact.INFERRED}.get(fact.basis)
    if other is None:
        return PersonFact.objects.none()
    return (PersonFact.objects.filter(person_id=fact.person_id, workspace_id=fact.workspace_id,
                                      category=fact.category, dimension=fact.dimension,
                                      basis=other, superseded_at__isnull=True,
                                      retracted_at__isnull=True)
            .exclude(entry_id=fact.entry_id))


def detect_conflicts(fact: PersonFact, *, actor: Actor) -> list[PersonFact]:
    """Evaluate on every write, symmetrically (2.6.1). The INFERRED side is
    quarantined; the declared side stays authoritative. Returns the entries
    newly marked conflicted."""
    marked = []
    if fact.basis == PersonFact.INFERRED and fact.status == PersonFact.ACTIVE:
        declared = _candidates(fact).filter(status=PersonFact.ACTIVE).first()
        if declared is not None:
            PersonFact.objects.filter(pk=fact.pk).update(status=PersonFact.CONFLICTED)
            fact.status = PersonFact.CONFLICTED
            marked.append(fact)
            audit(fact.person, "conflict.detected", actor=actor, entry=fact,
                  related_entry_id=declared.entry_id,
                  detail=f"dimension {fact.dimension!r}: inferred entry quarantined")
    elif fact.basis == PersonFact.DECLARED and fact.status == PersonFact.ACTIVE:
        for inferred in _candidates(fact).filter(status=PersonFact.ACTIVE):
            PersonFact.objects.filter(pk=inferred.pk).update(status=PersonFact.CONFLICTED)
            inferred.status = PersonFact.CONFLICTED
            marked.append(inferred)
            audit(fact.person, "conflict.detected", actor=actor, entry=inferred,
                  related_entry_id=fact.entry_id,
                  detail=f"dimension {fact.dimension!r}: inferred entry quarantined")
    return marked


def release_conflicts_against(declared: PersonFact, *, previous_dimension: str,
                              actor: Actor) -> None:
    """2.6.3 "superseding both": after the authoritative entry is revised,
    quarantined inferences it no longer conflicts with return to active."""
    if not previous_dimension or previous_dimension == declared.dimension:
        return
    stale = PersonFact.objects.filter(
        person_id=declared.person_id, workspace_id=declared.workspace_id,
        category=declared.category, dimension=previous_dimension, basis=PersonFact.INFERRED,
        status=PersonFact.CONFLICTED, superseded_at__isnull=True, retracted_at__isnull=True)
    for inferred in stale:
        still = (PersonFact.objects.filter(
            person_id=declared.person_id, workspace_id=declared.workspace_id,
            category=inferred.category, dimension=inferred.dimension, basis=PersonFact.DECLARED,
            status=PersonFact.ACTIVE, superseded_at__isnull=True, retracted_at__isnull=True)
            .exists())
        if still:
            continue
        PersonFact.objects.filter(pk=inferred.pk).update(status=PersonFact.ACTIVE)
        audit(declared.person, "conflict.resolved", actor=actor, entry=inferred,
              related_entry_id=declared.entry_id, detail="superseded: no longer conflicts")


# --- writes ----------------------------------------------------------------------


@transaction.atomic
def add_entry(*, person: Person, workspace, category: str, statement: str,
              declaration: str, actor: Actor, confidence: str | None = None,
              dimension: str | None = None, kind: str | None = None, source: str = "",
              source_turn=None, by_user=None, project=None, instance_ref: str = "",
              expires_at=None, relationship: str = "", metadata: dict | None = None,
              user_verified: bool = False, captured_by_override: str = "") -> PersonFact:
    """addPreference (3.2.2 / 3.3.2): a new entry at version 1, audited, and
    conflict-checked."""
    from .. import people

    category = (category or "").strip()
    if not valid_category(category):
        raise malformed(f"category {category!r} is not one canopy holds; one of "
                        f"{list(HELD_CATEGORIES)} or {PersonFact.CUSTOM_PREFIX}<name>")
    basis = BASIS_OF.get(declaration)
    if basis is None:
        raise malformed("declarationType is user-declared, model-inferred or issuer-attested")
    confidence = _check_confidence(basis, confidence)
    kind = kind or CATEGORY_KIND.get(category, PersonFact.PREFERENCE)
    try:
        fact = people.record_fact(
            person=person, workspace=workspace, kind=kind, statement=statement, basis=basis,
            by_user=by_user if actor.type == PersonAuditEvent.USER else None,
            by_agent=actor.agent, source_turn=source_turn, project=project,
            instance_ref=instance_ref, category=category, dimension=dimension,
            confidence=confidence, provenance_source=source, expires_at=expires_at,
            relationship=relationship, metadata=metadata, user_verified=user_verified,
            captured_by=captured_by_override or actor.id, actor=actor)
    except ZdrRefused as exc:
        raise denied(str(exc)) from None
    except people.FactError as exc:
        raise malformed(str(exc)) from None
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
                 by_user=None, source_turn=None, model: str | None = None) -> PersonFact:
    """updatePreference (3.2.3 / 3.3.3): a NEW VERSION of the same entry.

    * the person updating an inference ACCEPTS it (2.6.3): it becomes
      user-declared and active, and the declared entry it contradicted is
      deprecated;
    * a category change moves the entry between grant scopes, so only the
      person may make it (2.2), and it is audited with both categories.
    """
    from .. import people

    if fact.retracted_at is not None or fact.superseded_at is not None:
        raise denied()
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
            check_conflicts=not (was_conflicted and actor.is_person))
    except ZdrRefused as exc:
        raise denied(str(exc)) from None
    except people.FactError as exc:
        raise malformed(str(exc)) from None
    if was_conflicted and actor.is_person:
        # Accepting the inference: the previously authoritative entry is deprecated.
        for declared in (PersonFact.objects.filter(
                person_id=new.person_id, workspace_id=new.workspace_id, category=fact.category,
                dimension=fact.dimension, basis=PersonFact.DECLARED, status=PersonFact.ACTIVE,
                superseded_at__isnull=True, retracted_at__isnull=True)
                .exclude(entry_id=new.entry_id)):
            PersonFact.objects.filter(pk=declared.pk).update(status=PersonFact.DEPRECATED)
            audit(new.person, "conflict.resolved", actor=actor, entry=new,
                  related_entry_id=declared.entry_id,
                  detail="accepted: the inference is now declared; the prior entry is deprecated")
    elif new.basis == PersonFact.DECLARED:
        release_conflicts_against(new, previous_dimension=fact.dimension, actor=actor)
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
    if hard:
        PersonFact.objects.filter(entry_id=entry_id).delete()
        audit(person, "preference.hardDeleted", actor=actor, entry_id=entry_id, category=category,
              workspace_slug=fact.workspace_id, detail=reason)
    else:
        if fact.retracted_at is not None:
            return
        people.retract(fact, by=by_user, actor=actor, reason=reason)
    if was_conflicted:
        audit(person, "conflict.resolved", actor=actor, entry_id=entry_id, category=category,
              workspace_slug=fact.workspace_id, detail="rejected: the inference was deleted")
