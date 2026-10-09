"""Human Context Protocol (HCP) v1 — canopy's implementation of draft 4.

Spec: "Human Context Protocol (HCP) v1 — Draft Reference Specification",
1.0-draft-4 (Stanford HAI, Sept 2026). Profile implemented here:

* **HCP v1 Core, Tier 1 only, grouped envelope, `authorization_profile:
  first-party`.** Tier 2 (Verifiable Credentials) is optional at v1 and not
  built. OAuth wire flows are not offered (5.2.1): canopy only serves clients
  it operates — its own agents — so it is not eligible for Interop.
* **An entry is the people brain's `PersonFact` chain** (`entry_id` shared by
  every version). The brain was built two days before this spec arrived and is
  reshaped to it rather than duplicated beside it.
* **Grants are presumed by the control plane** (owner policy, 2026-10-08):
  canopy decides which agent serves which caller, so the first time a client
  reaches a person it issues the grant itself and audits it. A grant's client
  is (agent, channel, host) — see `PersonGrant`. A revoked grant is never
  re-presumed.
* **Relevance is lexical** (4.4.3): term overlap between the query and the
  entry, with a person's corrections and role always relevant to their own turn.

Every rule a route could get subtly wrong is a function here, so REST, MCP and
the turn envelope apply the same one.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Person, PersonAuditEvent, PersonFact, PersonGrant

logger = logging.getLogger(__name__)

HCP_VERSION = "1.0"
CONTEXT = ["https://www.w3.org/ns/credentials/v2", "https://hcp.me/contexts/v1"]
PROBLEM_BASE = "https://hcp.me/problems/"

#: The nine registered categories (2.5) — what discovery may name.
REGISTERED_CATEGORIES = (
    "general_preferences", "goals_and_constraints", "health_context",
    "education_learner_profile", "work_context", "purchase_history_preferences",
    "values_and_ethics", "location_context", "coordination_context",
)
#: The ones canopy HOLDS: work context only (see PersonFact.CATEGORIES).
HELD_CATEGORIES = PersonFact.CATEGORIES
ACTIONS = ("read", "write")
DEFAULT_MAX_ENTRIES = 5
MAX_ENTRIES = 20                       # 4.4.2, normative on every transport
MINIMIZATION_METHOD = "lexical"
#: 4.4.4 — never shown to an agent; the person sees them.
REDACTED_FIELDS = ["record.provenance.source", "record.provenance.capturedBy"]

KIND_CATEGORY = {
    PersonFact.ROLE: PersonFact.CAT_WORK, PersonFact.PROJECT: PersonFact.CAT_WORK,
    PersonFact.INSTANCE: PersonFact.CAT_WORK, PersonFact.PREFERENCE: PersonFact.CAT_GENERAL,
    PersonFact.CORRECTION: PersonFact.CAT_GENERAL, PersonFact.TERMINOLOGY: PersonFact.CAT_GENERAL,
}
CATEGORY_KIND = {
    PersonFact.CAT_WORK: PersonFact.ROLE, PersonFact.CAT_GENERAL: PersonFact.PREFERENCE,
    PersonFact.CAT_GOALS: PersonFact.PROJECT, PersonFact.CAT_COORDINATION: PersonFact.PREFERENCE,
}
DECLARATION = {PersonFact.DECLARED: "user-declared", PersonFact.INFERRED: "model-inferred",
               PersonFact.ATTESTED: "issuer-attested"}
BASIS_OF = {v: k for k, v in DECLARATION.items()}


class HcpError(Exception):
    """A request the protocol refuses. `problem` is a registered type (3.4.4)."""

    STATUS = {"invalid-token": 401, "scope-denied": 403, "malformed-request": 422,
              "idempotency-conflict": 409, "rate-limited": 429, "unsupported-version": 400}

    def __init__(self, problem: str, detail: str = ""):
        super().__init__(detail or problem)
        self.problem = problem
        self.status = self.STATUS[problem]
        self.detail = detail


def denied(detail: str = "outside the authorized scopes, or not found") -> HcpError:
    # 3.3.6: a missing entry and an unauthorized one look the same.
    return HcpError("scope-denied", detail)


def malformed(detail: str) -> HcpError:
    return HcpError("malformed-request", detail)


# --- the person's own switches: may agents learn about them, and use it? -------------

#: What an agent is told when the switch an operation needs is off. `scope-denied`,
#: the registered type for "outside what you are authorized for" (3.4.4).
RECORD_OFF = "this person has not let agents learn about them"
USE_OFF = "this person has not let agents use what they have learned"
#: Which switch an operation needs: "record" for a write, "use" for a read,
#: "any" for one that serves no entry content (an agent listing its own grant).
RECORD, USE, ANY = "record", "use", "any"


def may_record(person: Person) -> bool:
    return bool(person.hcp_record)


def may_use(person: Person) -> bool:
    return bool(person.hcp_use)


def require(person: Person, need: str) -> None:
    """Raise `scope-denied` unless the person's switch for `need` is on."""
    if need == RECORD and not may_record(person):
        raise denied(RECORD_OFF)
    if need == USE and not may_use(person):
        raise denied(USE_OFF)
    if need == ANY and not (may_record(person) or may_use(person)):
        raise denied(RECORD_OFF + ", nor use what they have learned")


def set_agent_memory(person: Person, *, actor: Actor, record: bool | None = None,
                     use: bool | None = None) -> list[str]:
    """Change the person's switches, as the person. `None` leaves one alone.
    Returns the audit event types written — one per switch that actually changed."""
    now, fields, events = timezone.now(), [], []
    for name, value, event in (("hcp_record", record, "agentRecord"), ("hcp_use", use, "agentUse")):
        if value is None or bool(getattr(person, name)) == bool(value):
            continue
        setattr(person, name, bool(value))
        setattr(person, f"{name}_changed_at", now)
        fields += [name, f"{name}_changed_at"]
        events.append(f"{event}.{'enabled' if value else 'disabled'}")
    if not fields:
        return []
    details = {
        "agentRecord.enabled": "agents may now record what they learn about you",
        "agentRecord.disabled": "agents may no longer record anything about you; nothing was deleted",
        "agentUse.enabled": "agents may now be told what has been learned about you",
        "agentUse.disabled": "agents are no longer told anything learned about you; nothing was deleted",
    }
    with transaction.atomic():
        person.save(update_fields=fields)
        for event in events:
            audit(person, event, actor=actor, detail=details[event])
    return events


# --- zero data retention: nothing from a ZDR session is ever written ------------------
#
# Owner rule (Jonathan, 2026-10-08): "if we are talking to a zdr runner, nothing should
# ever be written to this from those sessions." A ZDR conversation exists so that what is
# said in it is not kept; an entry, or an agent's inference from it, would keep it here.
#
# A turn is ZDR when its conversation REQUIRES ZDR (the host's signed requirement,
# `harness.runner_requirements`) or it was CLAIMED by a runner whose owner declared
# `zdr` (`RunnerFlag`) — and so is any turn descended from one (`parent_turn`), since a
# dispatch carries the session's words onward. Fail closed: a malformed requirement
# counts as ZDR, and an AGENT write that names no turn at all is refused, because it
# cannot be shown not to come from one.

ZDR = "zdr"
_ANCESTRY_MAX = 20


class ZdrRefused(Exception):
    """A write from a zero-data-retention session. Safe to show the caller."""


def _turn_is_zdr(turn) -> bool:
    from apps.harness import runner_requirements as rr
    from apps.harness.models import RunnerFlag

    reqs = rr.requirements_of(turn)
    if ZDR in reqs or rr.UNSATISFIABLE in reqs:
        return True
    return bool(turn.claimed_by_id) and RunnerFlag.objects.filter(
        runner_id=turn.claimed_by_id, flag=ZDR).exists()


def is_zdr_turn(turn) -> bool:
    """Is this turn — or any turn it descends from — a ZDR session's?"""
    seen = set()
    while turn is not None and turn.pk not in seen and len(seen) < _ANCESTRY_MAX:
        seen.add(turn.pk)
        if _turn_is_zdr(turn):
            return True
        turn = turn.parent_turn if turn.parent_turn_id else None
    return False


def _context_turn():
    """The turn the request in flight was made from (`X-Canopy-Parent-Turn`, which the
    canopy CLI fills from CANOPY_TURN_ID, or a caller token's own turn)."""
    from apps.common import request_context
    from apps.harness.models import Turn

    tid = ((request_context.current().get("parent") or {}).get("turn") or "").strip()
    if not tid:
        return None
    try:
        return Turn.objects.select_related("chat_session").filter(pk=uuid.UUID(tid)).first()
    except ValueError:
        return None


def refuse_if_zdr(*, source_turn=None, by_agent=None) -> None:
    """Raise ZdrRefused when this write comes from a ZDR session — or is an agent's write
    that cannot say which session it comes from."""
    turns = [t for t in (source_turn, _context_turn()) if t is not None]
    if any(is_zdr_turn(t) for t in turns):
        raise ZdrRefused("this session runs under zero data retention: nothing from it "
                         "is written to what canopy knows about people")
    if by_agent is not None and not turns:
        raise ZdrRefused("an agent's write must name the turn it comes from "
                         "(source turn, or X-Canopy-Parent-Turn), so a zero-data-retention "
                         "session can be told apart")


# --- who is acting -----------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """The `actorId`/`actorType` of an audit event, and whether 4.4.4 redacts."""

    id: str
    type: str                  # user | agent | system
    agent: object = None       # the Agent, when type == agent

    @property
    def is_person(self) -> bool:
        return self.type == PersonAuditEvent.USER


SYSTEM = Actor("canopy", PersonAuditEvent.SYSTEM)


def agent_actor(agent) -> Actor:
    return Actor(f"agent:{agent.slug}", PersonAuditEvent.AGENT, agent)


#: A model id an agent names for its inference (2.2.2): short, no spaces or slashes.
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def captured_by_model(actor: Actor, *, inferred: bool, model: str | None) -> str:
    """`record.provenance.capturedBy` for an agent's write (2.2.2).

    For a model-inferred entry the spec says capturedBy SHOULD name the model
    that made the inference, down to its version, not just the application: a
    confidence means nothing without knowing what produced it. So an agent that
    names its `model` is recorded as `agent:<slug>/model:<model>`. A person's
    write, a declared entry, or no model leaves the actor's own id."""
    model = (model or "").strip()
    if not model or not inferred or actor.is_person:
        return actor.id
    if not _MODEL_ID.match(model):
        raise malformed("model is a model id like claude-opus-5-5 (letters, digits, . _ : -; ≤100)")
    return f"{actor.id}/model:{model}"


def user_actor(user) -> Actor:
    return Actor(f"user:{getattr(user, 'email', '') or user.pk}", PersonAuditEvent.USER)


# --- vocabulary ------------------------------------------------------------------


def normalize_dimension(value: str | None) -> str:
    """2.6.1: trim, lowercase, internal whitespace → one underscore."""
    return "_".join((value or "").strip().lower().split())


def valid_category(category: str) -> bool:
    if category in HELD_CATEGORIES:
        return True
    return category.startswith(PersonFact.CUSTOM_PREFIX) and len(category) > len(PersonFact.CUSTOM_PREFIX)


def scope(category: str, action: str) -> str:
    return f"hcp:{category}:{action}"


def default_scopes() -> list[str]:
    """What a presumed grant covers: read and write on every held category.
    Custom categories are never included — wildcards are not permitted (4.1.1)."""
    return [scope(c, a) for c in HELD_CATEGORIES for a in ACTIONS]


def entry_urn(entry_id) -> str:
    return f"urn:uuid:{entry_id}"


def parse_entry_id(raw: str) -> uuid.UUID:
    text = (raw or "").strip()
    if text.lower().startswith("urn:uuid:"):
        text = text[9:]
    try:
        return uuid.UUID(text)
    except ValueError:
        raise denied() from None


# --- the audit log (4.3) ----------------------------------------------------------


def audit(person: Person, event_type: str, *, actor: Actor, entry: PersonFact | None = None,
          entry_id=None, related_entry_id=None, category: str = "", purpose: str = "",
          grant: PersonGrant | None = None, workspace_slug: str | None = None, turn=None,
          detail: str = "") -> PersonAuditEvent:
    """Append one event. Durable before the operation is acknowledged (4.3.4):
    it is written in the caller's transaction, so a failed write fails the call."""
    assert event_type in PersonAuditEvent.EVENT_TYPES, event_type
    if entry is not None:
        entry_id = entry.entry_id
        category = category or entry.category
        workspace_slug = workspace_slug or entry.workspace_id
    return PersonAuditEvent.objects.create(
        person=person, event_type=event_type, actor_id=actor.id[:200], actor_type=actor.type,
        entry_id=entry_id, related_entry_id=related_entry_id, category=category[:80],
        purpose=(purpose or "")[:500], grant_id=grant.grant_id if grant is not None else None,
        workspace_id=workspace_slug or None, turn=turn, detail=detail or "")


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


def to_entry(fact: PersonFact, *, redact: bool) -> dict:
    """The grouped Common Envelope (2.2). `redact` drops the 4.4.4 fields."""
    provenance = {"source": provenance_source(fact), "capturedAt": _iso(fact.created_at),
                  "capturedBy": captured_by(fact), "userVerified": bool(fact.user_verified)}
    if redact:
        provenance.pop("source")
        provenance.pop("capturedBy")
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
            "issuer": {"id": f"https://canopy.dimagi.com/w/{fact.workspace_id}", "type": "HCPSource"},
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
    meta["canopy:workspace"] = fact.workspace_id
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
    from . import people

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
    from . import people

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
    from . import people

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


# --- grants (4.1) ----------------------------------------------------------------


def client_of_turn(turn) -> tuple[str, str]:
    """(channel, host) a turn reached its agent through. The channel is how the
    person came in (`initiator_via`: slack, email, chat, mcp …); the host is
    the connected site when they came through an embedding SDK/widget."""
    channel = (getattr(turn, "initiator_via", "") or "").strip().lower()[:40]
    host = ""
    contact = getattr(turn, "initiator_contact", None) if getattr(turn, "initiator_contact_id", None) else None
    app = getattr(contact, "app", None) if contact is not None else None
    if app is not None:
        host = (getattr(app, "origin", "") or getattr(app, "name", "") or str(app.pk))[:200]
    return channel, host


def client_key(agent, channel: str, host: str, attributes: dict | None = None) -> str:
    parts = [f"agent={agent.slug if agent is not None else ''}", f"channel={channel}", f"host={host}"]
    for k in sorted(attributes or {}):
        parts.append(f"{k}={attributes[k]}")
    return "|".join(parts)[:400]


def client_name(agent, channel: str, host: str) -> str:
    """A name the person recognizes (4.1.5): "ACE over Slack", "Echo on example.org"."""
    who = (agent.name or agent.slug) if agent is not None else "canopy"
    where = f" on {host}" if host else (f" over {channel.capitalize()}" if channel else "")
    return f"{who}{where}"[:200]


def _expire_if_due(grant: PersonGrant) -> PersonGrant:
    if (grant.status == PersonGrant.ACTIVE and grant.expires_at is not None
            and grant.expires_at <= timezone.now()):
        PersonGrant.objects.filter(pk=grant.pk, status=PersonGrant.ACTIVE).update(
            status=PersonGrant.EXPIRED)
        grant.status = PersonGrant.EXPIRED
        audit(grant.person, "grant.expired", actor=SYSTEM, grant=grant,
              workspace_slug=grant.workspace_id, detail=grant.client_name)
    return grant


@transaction.atomic
def grant_for(person: Person, *, agent, workspace_slug: str, channel: str = "", host: str = "",
              attributes: dict | None = None, presume: bool = True) -> PersonGrant | None:
    """The active grant for this client, presuming one if the client has never
    had one. None when the person revoked it (or it expired): never re-presumed."""
    key = client_key(agent, channel, host, attributes)
    latest = (PersonGrant.objects.select_for_update().select_related("person")
              .filter(person=person, client_key=key, workspace_id=workspace_slug)
              .order_by("-issued_at", "-pk").first())
    if latest is not None:
        latest = _expire_if_due(latest)
        return latest if latest.status == PersonGrant.ACTIVE else None
    if not presume:
        return None
    grant = PersonGrant.objects.create(
        person=person, workspace_id=workspace_slug, agent=agent, channel=channel, host=host,
        attributes=attributes or {}, client_key=key, client_name=client_name(agent, channel, host),
        scopes=default_scopes(), grant_type=PersonGrant.PERSISTENT,
        modality=PersonGrant.MODALITY_CONTROL_PLANE)
    audit(person, "grant.issued", actor=SYSTEM, grant=grant, workspace_slug=workspace_slug,
          detail=(f"client={grant.client_name}; key={key}; type=persistent; "
                  f"modality={grant.modality} (presumed by the canopy control plane); "
                  f"scopes={' '.join(grant.scopes)}; restrictions={grant.restrictions or []}"))
    return grant


def categories_for(grant: PersonGrant, action: str) -> list[str]:
    out = []
    for s in grant.scopes or []:
        parts = s.split(":")
        if len(parts) >= 3 and parts[0] == "hcp" and parts[-1] == action:
            out.append(":".join(parts[1:-1]))
    return out


def allows(grant: PersonGrant | None, category: str, action: str) -> bool:
    return grant is not None and scope(category, action) in (grant.scopes or [])


def _restricted_entry_ids(grant: PersonGrant, category: str, action: str) -> set[str] | None:
    """4.1.5.1 Rule 1/4: a restriction only narrows. None = unrestricted."""
    ids: set[str] | None = None
    for r in grant.restrictions or []:
        if r.get("scope") != scope(category, action):
            continue
        named = (r.get("narrowedTo") or {}).get("entryIds")
        if named is None:
            continue
        found = {str(parse_entry_id(x)) for x in named if x}
        ids = found if ids is None else ids & found
    return ids


def within_grant(fact: PersonFact, grant: PersonGrant | None, action: str) -> bool:
    if grant is None or fact.workspace_id != grant.workspace_id:
        return False
    if not allows(grant, fact.category, action):
        return False
    ids = _restricted_entry_ids(grant, fact.category, action)
    return ids is None or str(fact.entry_id) in ids


@transaction.atomic
def revoke_grant(grant: PersonGrant, *, actor: Actor) -> PersonGrant:
    """4.2: effective immediately — every read checks the grant row, so there is
    no token to chase. No webhook is registered by any canopy client, so there
    is nothing to notify (4.2.3)."""
    if grant.status == PersonGrant.ACTIVE:
        PersonGrant.objects.filter(pk=grant.pk).update(status=PersonGrant.REVOKED,
                                                       revoked_at=timezone.now())
        grant.refresh_from_db()
        audit(grant.person, "grant.revoked", actor=actor, grant=grant,
              workspace_slug=grant.workspace_id, detail=grant.client_name)
    return grant


def grant_dict(grant: PersonGrant) -> dict:
    return {
        "grantId": entry_urn(grant.grant_id),
        "client": {"id": grant.client_key, "name": grant.client_name},
        "scopes": list(grant.scopes or []),
        "restrictions": list(grant.restrictions or []),
        "grantType": grant.grant_type,
        "grantor": grant.grantor or None,
        "issuedAt": _iso(grant.issued_at),
        "expiresAt": _iso(grant.expires_at),
        "status": grant.status,
        # canopy's own: the dimensions the client key is built from.
        "canopy": {"agent": grant.agent.slug if grant.agent_id else None,
                   "channel": grant.channel, "host": grant.host,
                   "workspace": grant.workspace_id, "modality": grant.modality},
    }


# --- search (3.1, 4.4) -----------------------------------------------------------

_WORD = re.compile(r"[a-z0-9][a-z0-9_'-]{2,}")
_STOP = frozenset("""the and for are but not you your yours with this that these those from have has had was
were what when where which who whom why how can could would should will shall may might must does did
doing about into onto over under than then them they their there here our ours its it's i'm i've i'd
any all some each very just also only more most such own same too out off again once""".split())
#: A person's own corrections and role are relevant to anything they ask.
ALWAYS_RELEVANT = frozenset({PersonFact.CORRECTION, PersonFact.ROLE})


def _terms(text: str) -> set[str]:
    return {w.strip("'-") for w in _WORD.findall((text or "").lower())} - _STOP


def _score(fact: PersonFact, query_terms: set[str]) -> int:
    hay = " ".join(filter(None, [fact.statement, fact.instance_ref, fact.dimension.replace("_", " "),
                                 fact.project.name if fact.project_id else ""]))
    return len(query_terms & _terms(hay))


def search(person: Person, *, workspace_slug: str, query: str, categories: list[str],
           purpose: str, max_entries: int, grant: PersonGrant | None, actor: Actor,
           turn=None) -> tuple[list[PersonFact], list[str], str]:
    """searchPreferences. Returns (entries, categoriesSearched, retrievalId).

    4.4.1 — only categories the grant reads (the person themself: their own
    held categories); an out-of-scope category is a 403, not a silent drop.
    4.4.2 — at most min(maxEntries, 20), never padded. 4.4.3 — lexical order.
    Every call appends `preference.read` (one per returned entry, or one for
    an empty result), with the stated purpose.
    """
    if not (purpose or "").strip():
        raise malformed("purpose is required")
    if not isinstance(max_entries, int) or max_entries < 1:
        raise malformed("maxEntries is a positive integer")
    limit = min(max_entries, MAX_ENTRIES)
    wanted = [c.strip() for c in (categories or []) if c and c.strip()]
    if not wanted:
        raise malformed("categories is required")
    if actor.is_person:
        readable = set(HELD_CATEGORIES) | {c for c in wanted if valid_category(c)}
    else:
        readable = set(categories_for(grant, "read")) if grant is not None else set()
    outside = [c for c in wanted if c not in readable]
    if outside:
        raise denied(f"not authorized for {outside}")
    qs = servable(PersonFact.objects.filter(person=person, workspace_id=workspace_slug,
                                            category__in=wanted)).select_related("project", "person",
                                                                                 "workspace")
    candidates = [f for f in qs if actor.is_person or within_grant(f, grant, "read")]
    terms = _terms(query)
    scored = []
    for f in candidates:
        s = _score(f, terms)
        if s > 0 or f.kind in ALWAYS_RELEVANT:
            scored.append((0 if f.kind == PersonFact.CORRECTION else 1, -s, -f.created_at.timestamp(), f))
    scored.sort(key=lambda t: t[:3])
    hits = [t[3] for t in scored[:limit]]
    retrieval = str(uuid.uuid4())
    detail = f"retrievalId=urn:uuid:{retrieval}; returned={len(hits)}"
    if hits:
        for f in hits:
            audit(person, "preference.read", actor=actor, entry=f, purpose=purpose, grant=grant,
                  turn=turn, detail=detail)
    else:
        audit(person, "preference.read", actor=actor, category=",".join(wanted)[:80], purpose=purpose,
              grant=grant, workspace_slug=workspace_slug, turn=turn, detail=detail)
    return hits, wanted, retrieval


def minimization(actor: Actor) -> dict:
    return {"method": MINIMIZATION_METHOD,
            "redactedFields": [] if actor.is_person else list(REDACTED_FIELDS)}


def read_one(fact: PersonFact, *, actor: Actor, grant: PersonGrant | None, purpose: str,
             turn=None) -> None:
    """GET /v1/preferences/{id} (3.3.6): authorized and audited like a search."""
    if not (purpose or "").strip():
        raise malformed("purpose is required")
    audit(fact.person, "preference.read", actor=actor, entry=fact, purpose=purpose, grant=grant,
          turn=turn, detail=f"single entry, version {fact.version}")


# --- discovery (Appendix C) ------------------------------------------------------


def discovery(base_url: str) -> dict:
    return {
        "hcp_version": HCP_VERSION,
        "issuer": base_url,
        "preferences_endpoint": f"{base_url}/v1/preferences",
        "grants_endpoint": f"{base_url}/v1/grants",
        "audit_endpoint": f"{base_url}/v1/audit",
        "export_endpoint": f"{base_url}/v1/export",
        "mcp_endpoint": base_url.rsplit("/api/", 1)[0] + "/api/mcp/",
        "supported_categories": list(HELD_CATEGORIES),
        "supported_transports": ["mcp", "rest"],
        "conformance_level": "HCP-v1-Core",
        "authorization_profile": "first-party",
        "envelope_form": "grouped",
        "minimization_method": MINIMIZATION_METHOD,
        "audit_durability": {"default": "synchronous"},
        "pagination": {"audit": "cursor"},
        "problem_type_base": PROBLEM_BASE,
        "canopy": {
            "grants": "presumed by the canopy control plane per client (agent, channel, host); "
                      "revocable by the person; never re-presumed after revocation",
            "tier2": "not supported",
            "custom_categories": f"{PersonFact.CUSTOM_PREFIX}<name>",
        },
    }
