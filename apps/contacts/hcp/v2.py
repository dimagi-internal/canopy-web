"""HCP v2 (draft 3) as canopy's internal model: the one entry form, as stored.

The `PersonFact` chain is canopy's entry (docs/architecture/hcp-service.md, "HCP v2"). This module
renders it in the v2 form (4.2) for the person's own access: the Management Client, the export.
What a Context Client receives is a *disclosure*, never this (7.4); that is `disclosure.py`.

Identifiers are the draft's own placeholders, compared as exact strings (1.7); they move with the
spec, which `scripts/hcp_spec_probe.py` watches. Nothing here copies spec text: sections are cited.
"""
from __future__ import annotations

from ..models import PersonFact
from . import confirmations, relationships
from .core import categories_of, entry_urn
from .entries import _iso, captured_by, provenance_source, subject_id

HCP_VERSION_V2 = "2.0"
CONTEXT_V2 = ["https://hcp.example/contexts/v2"]
ENTRY_SCHEMA = "https://hcp.example/spec/entry/2.0"                                    # 4.2
GENERAL_CONTEXT = "https://hcp.example/spec/profiles/general-context/2.0"             # 11
#: v1's labels for the declaration type → v2's (4.4).
DECLARATION_V2 = {PersonFact.DECLARED: "person-declared", PersonFact.INFERRED: "model-inferred",
                  PersonFact.ATTESTED: "issuer-attested"}
BASIS_OF_V2 = {v: k for k, v in DECLARATION_V2.items()}

#: v1 `user-input` is v2 `person-input` (4.3).
_SOURCE_V2 = {"user-input": "person-input"}


def to_v2_entry(fact: PersonFact) -> dict:
    """The entry as the person sees it (4.2): `content`, `assertion`, `classification`, `evidence`,
    `record`, each kept apart. `userVerified` is gone (3.5); confirmations replace it (3.8)."""
    assertion = {"declarationType": DECLARATION_V2[fact.basis], "confidence": fact.confidence or None,
                 "validUntil": _iso(fact.expires_at)}
    if fact.basis_detail:
        assertion["basis"] = dict(fact.basis_detail)
    if fact.permitted_purposes is not None:
        assertion["permittedPurposes"] = list(fact.permitted_purposes)
    classification = {"categories": categories_of(fact), "dimension": fact.dimension or None}
    if fact.restricted_purposes is not None:
        classification["permittedPurposes"] = list(fact.restricted_purposes)
    source = provenance_source(fact)
    provenance = {
        "assertedBy": {"id": captured_by(fact), "assurance": assurance_of(fact)},
        "assertedAt": _iso(fact.created_at),
        "source": _SOURCE_V2.get(source, source),
        "recordedByProvider": "https://canopy.dimagi.com",
        "recordedAt": _iso(fact.created_at),
        "confirmations": confirmations.render(fact),
    }
    if fact.provenance_site:
        provenance["site"] = dict(fact.provenance_site)             # canopy's extension, never scope
    record = {"status": fact.status, "version": fact.version, "provenance": provenance,
              "relationships": relationships.render(fact)}
    return {
        "@context": CONTEXT_V2, "schema": ENTRY_SCHEMA, "id": entry_urn(fact.entry_id),
        "type": ["ContextEntry"],
        "subject": {"id": subject_id(fact.person), "relationship": fact.relationship or None},
        "content": {"profile": GENERAL_CONTEXT, "value": fact.statement, "language": None},
        "assertion": assertion, "classification": classification, "evidence": [], "record": record,
    }


def assurance_of(fact: PersonFact) -> str:
    """How sure canopy is of who asserted the entry (3.6), fixed when it was written
    (`PersonFact.assurance`): `authenticated` only for the person's own write, else `unverified`."""
    return fact.assurance


# --- the audit log in v2's shape (8.2) ---------------------------------------------------------

#: canopy's event names (v1's, kept for every existing reader) → v2's (8.1).
EVENT_V2 = {"preference.created": "context.created", "preference.read": "context.read",
            "preference.updated": "context.revised", "preference.deleted": "context.withdrawn",
            "preference.hardDeleted": "context.hardDeleted", "preference.exported": "context.exported"}
_ROLE_V2 = {"user": "management-client", "agent": "context-client", "system": "hcp-provider"}


def event_v2(e) -> dict:
    """One audit event as 8.2 shapes it. canopy's own event types (the agent-memory switches, the
    collections) are kept under their own names; `data` carries what is recorded in detail."""
    data = dict(e.data or {})
    if e.detail and not data:
        data["detail"] = e.detail
    out = {
        "eventId": entry_urn(e.event_id), "eventType": EVENT_V2.get(e.event_type, e.event_type),
        "timestamp": e.timestamp.isoformat(),
        "participants": {"initiator": {"role": _ROLE_V2[e.actor_type], "id": e.actor_id},
                         "provider": {"id": "https://canopy.dimagi.com"}},
        "entryId": entry_urn(e.entry_id) if e.entry_id else None,
        "relatedEntryId": entry_urn(e.related_entry_id) if e.related_entry_id else None,
        "categories": [e.category] if e.category else [],
        "purpose": e.purpose_code or None, "purposeDescription": e.purpose or None,
        "grantId": entry_urn(e.grant_id) if e.grant_id else None, "data": data,
    }
    if (e.data or {}).get("retrievalId"):
        out["participants"]["retrievalId"] = e.data["retrievalId"]
    if e.event_type == "preference.read" and e.entry_id and (e.data or {}).get("useUntil"):
        out["disclosed"] = [{"entryId": entry_urn(e.entry_id), "version": e.data.get("version"),
                             "useUntil": e.data["useUntil"]}]
    return out
