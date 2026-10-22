"""Shared vocabulary of the HCP v1 service: constants, the problem errors, who is
acting (`Actor`), scope and entry-id helpers, and the person's audit log (4.3)."""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from ..models import Person, PersonAuditEvent, PersonFact, PersonGrant

logger = logging.getLogger("apps.contacts.hcp")

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
#: Declared instead while embeddings are on (`embeddings.enabled`): meaning plus shared words.
HYBRID_METHOD = "hybrid"
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


def contact_actor(contact) -> Actor:
    """The person themself, acting through a contact token in canopy's own frame
    (`tokens/contact_hcp_api`) — a person's act, so `user`, named by the contact
    because there is no account."""
    return Actor(f"contact:{contact.pk}", PersonAuditEvent.USER)


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
    """Read and write on every held category.
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
