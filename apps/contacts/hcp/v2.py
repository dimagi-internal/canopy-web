"""HCP v2 (draft 3) as canopy's internal model: the one entry form, as stored.

The `PersonFact` chain is canopy's entry (docs/architecture/hcp-service.md, "HCP v2"). This module
renders it in the v2 form (4.2) for the person's own access: the Management Client, the export.
What a Context Client receives is a *disclosure*, never this (7.4); that is `disclosure.py`.

Identifiers are the draft's own placeholders, compared as exact strings (1.7); they move with the
spec, which `scripts/hcp_spec_probe.py` watches. Nothing here copies spec text: sections are cited.
"""
from __future__ import annotations

from ..models import PersonFact
from .core import entry_urn
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
        "confirmations": [],
    }
    if fact.provenance_site:
        provenance["site"] = dict(fact.provenance_site)             # canopy's extension, never scope
    record = {"status": fact.status, "version": fact.version, "provenance": provenance,
              "relationships": []}
    return {
        "@context": CONTEXT_V2, "schema": ENTRY_SCHEMA, "id": entry_urn(fact.entry_id),
        "type": ["ContextEntry"],
        "subject": {"id": subject_id(fact.person), "relationship": fact.relationship or None},
        "content": {"profile": GENERAL_CONTEXT, "value": fact.statement, "language": None},
        "assertion": assertion, "classification": classification, "evidence": [], "record": record,
    }


def categories_of(fact: PersonFact) -> list[str]:
    """The entry's categories (4.5): the one canopy stores first, then any added by the person."""
    return [fact.category, *[c for c in (fact.extra_categories or []) if c != fact.category]]


def assurance_of(fact: PersonFact) -> str:
    """3.6: how the asserter's identity was established, as the provider recorded it at write
    (`PersonFact.assurance`): the person's own write is `authenticated`, anyone else's `unverified`."""
    return fact.assurance
