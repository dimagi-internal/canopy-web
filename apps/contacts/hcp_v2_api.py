"""/api/hcp/v2 — the Human Context Protocol v2 (draft 3) over HTTP.

v2 is canopy's internal model (`hcp/v2.py`); v1 (`hcp_api.py`) stays the wire every existing caller
uses, as an adapter over the same rows. Here are the v2 routes, planed as the spec has them (1.6):

* **governance** (the person through a Management Client — their own session or PAT, never an
  agent's login, a caller token, an OAuth client's token or a contact's), HTTP only, never MCP;
* **context** (any client holding a grant that covers the operation).

Every route is HTTP-only for now: an MCP binding for v2 is later work, so each route is listed in
`apps/mcp/api_tools.py::EXCLUDED`. Errors are RFC 9457 problems with the registered types (9.7),
and every response carries `HCP-Version: 2.0` and no `hcp_version` in the body (9.3).
"""
from __future__ import annotations

import json
import uuid
from functools import wraps

from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from ninja import Router
from pydantic import Field

from apps.api.route_gates import gate

from . import hcp, hcp_api, hcp_oauth
from .hcp import v2
from .hcp.core import PROBLEM_BASE

router = Router(auth=hcp_api._HcpAuth(), tags=["hcp-v2"])
LD_JSON = "application/ld+json"


class _Body(hcp_api._Body):
    pass


def _ok(body: dict, status: int = 200) -> JsonResponse:
    resp = JsonResponse(body, status=status, content_type=LD_JSON, safe=not isinstance(body, list))
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


def _problem(request: HttpRequest, err: hcp.HcpError) -> HttpResponse:
    body = {"type": PROBLEM_BASE + err.problem, "title": err.problem.replace("-", " "),
            "status": err.status, "detail": err.detail or err.problem, "instance": request.path}
    resp = HttpResponse(json.dumps(body), status=err.status, content_type="application/problem+json")
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    if err.status == 429:
        resp["Retry-After"] = "60"
    return resp


def v2_route(view):
    """The version check (9.3) and HcpError → problem+json, on every v2 route."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        try:
            asked = (request.headers.get("HCP-Version") or "").strip()
            if asked and asked != v2.HCP_VERSION_V2:
                raise hcp.HcpError("unsupported-version", f"this is the v2 binding; it serves {v2.HCP_VERSION_V2}")
            return view(request, *args, **kwargs)
        except hcp.HcpError as err:
            return _problem(request, err)
    return wrapper


def _entry(p, entry_id: str, expected_version: int | None):
    """The person's own entry, current version; a stale `expectedVersion` is a version conflict (5.4)."""
    fact = hcp.current(hcp.parse_entry_id(entry_id))
    if fact is None or fact.person_id != p.person.pk:
        raise hcp.denied()
    if expected_version is not None and fact.version != expected_version:
        raise hcp.HcpError("version-conflict", f"the entry is at version {fact.version}")
    return fact


# --- governance: an entry's purposes (4.2.6, 9.4.10) ---------------------------------


class HcpV2PurposesIn(_Body):
    permittedPurposes: list[str] | None = Field(
        description="The purpose codes the entry may be used for, narrower than its writer permitted; "
                    "null removes your narrowing (never the writer's).")
    expectedVersion: int | None = Field(default=None, description="The version you are looking at.")


@router.put("/entries/{entry_id}/purposes", operation_id="hcp_v2_restrictPurposes",
            summary="HCP v2 governance: limit what an entry may be used for")
@v2_route
@gate("self", setting=True)  # the person only: a Management Client acting for the Authorizing Principal
def hcp_v2_restrict_purposes(request: HttpRequest, entry_id: str, payload: HcpV2PurposesIn):
    """Set or clear the Authorizing Principal's narrowing of an entry's permitted purposes (HCP v2
    4.2.6, a governance operation: the person, never an agent, over HTTP only). It can only narrow;
    recorded as `context.restricted` naming the lists before and after."""
    p = hcp_api._person_principal(request)
    fact = _entry(p, entry_id, payload.expectedVersion)
    fact = hcp.restrict_purposes(fact, payload.permittedPurposes, actor=p.actor)
    return _ok(v2.to_v2_entry(fact))


# --- governance: confirm or reject (3.8, 9.4.9) ---------------------------------------


class HcpV2ConfirmationIn(_Body):
    disposition: str = Field(description="confirmed or rejected.")
    expectedVersion: int | None = Field(default=None, description="The version you are looking at.")


@router.post("/entries/{entry_id}/confirmation", operation_id="hcp_v2_confirmEntry",
             summary="HCP v2 governance: confirm or reject an entry")
@v2_route
@gate("self", setting=True)  # the person only: a Management Client acting for the Authorizing Principal
def hcp_v2_confirm_entry(request: HttpRequest, entry_id: str, payload: HcpV2ConfirmationIn):
    """Confirm or reject the current version of one of your entries (HCP v2 3.8): the person only,
    over HTTP only. A confirmation appends a record to that version and gives it standing in a
    conflict; it changes neither the entry's declaration type nor who asserted it. A rejection
    withdraws the entry and returns anything it held to active (4.7.3)."""
    p = hcp_api._person_principal(request)
    fact = _entry(p, entry_id, payload.expectedVersion)
    fact = hcp.confirmations.confirm(fact, payload.disposition, actor=p.actor)
    return _ok(v2.to_v2_entry(fact))


# --- governance: standing rules (6.3, 9.4.14) and directives (6.4, 9.4.15) ----------------


class HcpV2RuleIn(_Body):
    operators: list[str] | None = Field(default=None, description="Client operators it denies: a DNS domain "
                                        "(that host and its subdomains) or one operator identifier.")
    requires: list[str] | None = Field(default=None, description="Client claims: it denies only clients that "
                                       "do not make every one. Required when `operators` is absent.")
    categories: list[str] | None = Field(default=None, description="Categories it applies to; absent = every one.")


class HcpV2DirectiveIn(_Body):
    action: str = Field(description="share, sign, transact, or an absolute URI.")
    effect: str = Field(description="deny, ask-first or permit.")
    text: str = Field(description="The directive in your own words: every client it applies to is shown it.")
    categories: list[str] | None = None
    operators: list[str] | None = None


@router.get("/rules", operation_id="hcp_v2_listRules", summary="HCP v2 governance: my standing rules")
@v2_route
@gate("self")
def hcp_v2_list_rules(request: HttpRequest):
    """Your standing rules (HCP v2 6.3): what you set once for every grant, now and later."""
    p = hcp_api._person_principal(request)
    return _ok({"rules": [hcp.rules.rule_dict(r) for r in p.person.standing_rules.all()]})


@router.post("/rules", operation_id="hcp_v2_setRule", summary="HCP v2 governance: set a standing rule")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_set_rule(request: HttpRequest, payload: HcpV2RuleIn):
    """Set a deny rule (HCP v2 6.3): enforced on every grant, existing and future, from now. A grant
    it covers entirely is revoked and its client notified. The person only, over HTTP only."""
    p = hcp_api._person_principal(request)
    rule = hcp.set_standing_rule(p.person, operators=payload.operators, requires=payload.requires,
                              categories=payload.categories, actor=p.actor)
    return _ok(hcp.rules.rule_dict(rule), status=201)


@router.delete("/rules/{rule_id}", operation_id="hcp_v2_removeRule", summary="HCP v2 governance: remove a standing rule")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_remove_rule(request: HttpRequest, rule_id: str):
    """Remove a standing rule you set. The only way to override one (6.3)."""
    p = hcp_api._person_principal(request)
    hcp.rules.remove_rule(p.person, hcp.parse_entry_id(rule_id), actor=p.actor)
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


@router.get("/directives", operation_id="hcp_v2_listDirectives", summary="HCP v2 governance: my directives")
@v2_route
@gate("self")
def hcp_v2_list_directives(request: HttpRequest):
    """Your directives (HCP v2 6.4): what clients may do with what they received."""
    p = hcp_api._person_principal(request)
    return _ok({"directives": [hcp.rules.directive_dict(d) for d in p.person.directives.all()]})


@router.post("/directives", operation_id="hcp_v2_setDirective", summary="HCP v2 governance: set a directive")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_set_directive(request: HttpRequest, payload: HcpV2DirectiveIn):
    """Set a directive (HCP v2 6.4). Every client it applies to receives its text with each result,
    so keep a directive about sensitive context to its categories. Directives only narrow."""
    p = hcp_api._person_principal(request)
    d = hcp.rules.set_directive(p.person, action=payload.action, effect=payload.effect, text=payload.text,
                                categories=payload.categories, operators=payload.operators, actor=p.actor)
    return _ok(hcp.rules.directive_dict(d), status=201)


@router.delete("/directives/{directive_id}", operation_id="hcp_v2_removeDirective",
               summary="HCP v2 governance: remove a directive")
@v2_route
@gate("self", setting=True)  # the person only
def hcp_v2_remove_directive(request: HttpRequest, directive_id: str):
    """Remove a directive you set (HCP v2 6.4)."""
    p = hcp_api._person_principal(request)
    hcp.rules.remove_directive(p.person, hcp.parse_entry_id(directive_id), actor=p.actor)
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


# --- the context plane (5.2, 9.4) ---------------------------------------------------------------
#
# Any client holding a grant that covers the operation: an agent for the person who started its
# turn (`turn`), or an OAuth client. A client receives *disclosures* (7.4), never stored entries; the
# person (a management client) may also read and write, and gets the entry itself.


def _disclosures(p, facts) -> list[dict]:
    refs = {f.entry_id: hcp.disclosure.ref_for(p.grant, f.entry_id) for f in facts}
    return [hcp.disclosure.to_disclosure(f, p.grant, refs=refs) for f in facts]


def _render_one(p, fact):
    if p.actor.is_person:
        return v2.to_v2_entry(fact)
    return hcp.disclosure.to_disclosure(fact, p.grant)


def _reachable(p, action: str):
    """The entries a client's grant reaches for `action`, current and servable, as they stand."""
    qs = hcp.servable(hcp.PersonFact.objects.filter(person=p.person)).select_related("person", "project")
    return [f for f in qs if hcp.within_grant(f, p.grant, action)]


def _by_name(p, name: str, action: str):
    """An entry named as its path says (3 / 9.2): the entry id for the person, its `ref` for a client.
    A missing entry and an unauthorized one fail the same way (5.2.2)."""
    if p.actor.is_person:
        fact = hcp.current(hcp.parse_entry_id(name))
        if fact is None or fact.person_id != p.person.pk:
            raise hcp.denied()
        return fact
    fact = hcp.disclosure.resolve_ref(p.grant, name, _reachable(p, action))
    if fact is None:
        raise hcp.denied()
    return fact       # the purpose is checked where the read is made (`hcp.read_one`, `hcp.search`)


class HcpV2QueryIn(_Body):
    query: str = Field(description="Natural language description of the context or task.")
    categories: list[str] | None = Field(default=None, description="Categories to query; omitted = every one the "
                                         "grant can read for the purpose named.")
    purpose: str = Field(description="A purpose code the grant approved for those categories.")
    purposeDescription: str | None = Field(default=None, description="Plain language statement of the task; "
                                           "recorded in the audit log.")
    maxEntries: int = Field(default=hcp.DEFAULT_MAX_ENTRIES, description="At most this many (ceiling 20).")


@router.post("/entries/query", operation_id="hcp_v2_queryContext",
             summary="HCP v2: recall what is relevant about the person you are serving")
@v2_route
@gate("self", "hcp-grant", kind="action")
def hcp_v2_query(request: HttpRequest, payload: HcpV2QueryIn, turn: str | None = None, workspace: str | None = None):
    """Query (HCP v2 5.2.1). A client receives disclosures: the content, the assertion, the categories
    its grant covers, `standing`, a grant-scoped `ref` and `useUntil` (7.4, 7.6), with the directives that
    apply. Never paginated, at most 20, never padded; every read is on the person's audit log."""
    p = hcp_api._principal(request, turn, hcp.USE)
    cats = payload.categories
    if not cats:
        cats = (list(hcp.HELD_CATEGORIES) if p.actor.is_person
                else hcp.rules.permitted(p.grant, hcp.categories_for(p.grant, "read")))
    hits, searched, retrieval = hcp.search(
        p.person, workspace_slugs=hcp_api._search_workspace(p, workspace), query=payload.query, categories=cats,
        purpose=payload.purposeDescription or payload.query[:200], max_entries=payload.maxEntries, grant=p.grant,
        actor=p.actor, turn=p.turn, purpose_code=None if p.actor.is_person else payload.purpose,
        project_id=(hcp.project_of(p.turn).pk if hcp.project_of(p.turn) is not None else None))
    out = {"entries": ([v2.to_v2_entry(f) for f in hits] if p.actor.is_person else _disclosures(p, hits)),
           "retrievalId": f"urn:uuid:{retrieval}", "minimization": {"method": hcp.minimization(p.actor)["method"]},
           "categoriesSearched": searched, "timestamp": timezone.now().isoformat()}
    if not p.actor.is_person:
        out["directives"] = hcp.rules.delivered(p.grant, hits)
    return _ok(out)


@router.get("/entries/{entry_id}", operation_id="hcp_v2_getEntry", summary="HCP v2: one entry")
@v2_route
@gate("self", "hcp-grant")
def hcp_v2_get_entry(request: HttpRequest, entry_id: str, purpose: str = "", purposeDescription: str = "",
                     turn: str | None = None):
    """Get entry (HCP v2 5.2.2): by `ref` for a client, by id for the person. 403 for a missing entry too."""
    p = hcp_api._principal(request, turn, hcp.USE)
    fact = _by_name(p, entry_id, "read")
    if not p.actor.is_person:
        hcp.read_one(fact, actor=p.actor, grant=p.grant, purpose=purposeDescription or "get entry", turn=p.turn,
                     purpose_code=purpose or None)
    out = {"entry": _render_one(p, fact), "retrievalId": f"urn:uuid:{uuid.uuid4()}",
           "minimization": {"method": "none"}, "timestamp": timezone.now().isoformat()}
    if not p.actor.is_person:
        out["directives"] = hcp.rules.delivered(p.grant, [fact])
    return _ok(out)


class HcpV2ContentIn(_Body):
    profile: str
    value: str
    language: str | None = None


class HcpV2AssertionIn(_Body):
    declarationType: str
    confidence: str | None = None
    validUntil: str | None = None
    basis: dict | None = None
    permittedPurposes: list[str] | None = None


class HcpV2ClassificationIn(_Body):
    categories: list[str]
    dimension: str | None = None


class HcpV2RelationshipIn(_Body):
    type: str
    target: str = Field(description="The target entry's `ref` (a client) or id (the person), or "
                                    "`urn:canopy:source:<kind>:<ref>` for a source a derivedFrom was drawn from.")


class HcpV2ProvenanceIn(_Body):
    assertedBy: dict | None = None
    assertedAt: str | None = None
    source: str


class HcpV2RecordIn(_Body):
    provenance: HcpV2ProvenanceIn
    relationships: list[HcpV2RelationshipIn] | None = None


class HcpV2SubjectIn(_Body):
    id: str
    relationship: str | None = None


class HcpV2EntryIn(_Body):
    schema_: str = Field(alias="schema", description="The entry structure version (4.2).")
    subject: HcpV2SubjectIn
    content: HcpV2ContentIn
    classification: HcpV2ClassificationIn
    assertion: HcpV2AssertionIn
    evidence: list[dict] | None = None
    record: HcpV2RecordIn


_BASIS_KEYS = {"drawnFrom", "task", "setting", "rationale"}


def _basis(raw: dict | None) -> dict:
    """4.2.5: `drawnFrom` (kinds of material), and optional words. Free text cannot be checked against the
    rule that it names no party, client, grant, conversation or document; the writing client is
    responsible for that, as the spec says."""
    if not raw:
        return {}
    if not isinstance(raw, dict) or set(raw) - _BASIS_KEYS:
        raise hcp.malformed(f"basis has only {sorted(_BASIS_KEYS)}")
    out = {}
    if "drawnFrom" in raw:
        if not isinstance(raw["drawnFrom"], list) or not all(isinstance(x, str) for x in raw["drawnFrom"]):
            raise hcp.malformed("basis.drawnFrom is a list of words")
        out["drawnFrom"] = raw["drawnFrom"]
    for k in ("task", "setting", "rationale"):
        if raw.get(k) is not None:
            out[k] = str(raw[k])[:500]
    return out


def _target(p, name: str):
    """(entry, None) or (None, (kind, ref)) for a relationship target (4.2.3)."""
    if name.startswith(hcp.relationships.SOURCE_URN):
        kind, _, ref = name[len(hcp.relationships.SOURCE_URN):].partition(":")
        return None, (kind, ref)
    return _by_name(p, name, "read"), None


@router.post("/entries", operation_id="hcp_v2_createEntry", summary="HCP v2: remember something about the person")
@v2_route
@gate("self", "hcp-grant", kind="person-data")
def hcp_v2_create_entry(request: HttpRequest, payload: HcpV2EntryIn, turn: str | None = None,
                        workspace: str | None = None):
    """Create (HCP v2 5.2.3): about the person who started `turn`, in categories your grant may write.
    `model-inferred` needs a `confidence`. A client's `person-declared` is recorded `unverified` (3.7):
    only the person's own write is `authenticated`. `issuer-attested` is only ever from the issuer, which
    canopy has no way to authenticate, so it is refused. Relationships: `derivedFrom` (an entry or a
    source), `duplicates`, `supersedes`, `contradicts`."""
    p = hcp_api._principal(request, turn, hcp.RECORD)
    body = payload.model_dump(by_alias=True)
    if payload.schema_ != v2.ENTRY_SCHEMA:
        raise hcp.malformed(f"schema is {v2.ENTRY_SCHEMA}")
    if payload.subject.id != hcp.subject_id(p.person):
        raise hcp.malformed("subject.id is the person this instance holds")
    if payload.content.profile != v2.GENERAL_CONTEXT:
        raise hcp.malformed(f"content.profile is {v2.GENERAL_CONTEXT}")
    if payload.evidence:
        raise hcp.malformed("this instance holds no evidence items")
    decl = v2.BASIS_OF_V2.get(payload.assertion.declarationType)
    if decl is None or decl == hcp.PersonFact.ATTESTED:
        if decl == hcp.PersonFact.ATTESTED and not p.actor.is_person:
            raise hcp.denied("only the issuer may attest, and this instance cannot authenticate one")
        raise hcp.malformed("declarationType is person-declared or model-inferred")
    cats = [c for c in dict.fromkeys(payload.classification.categories)]
    if not cats:
        raise hcp.malformed("classification.categories is a non-empty list")
    for c in cats:
        if not hcp.valid_category(c):
            raise hcp.malformed(f"{c!r} is not a category this instance holds")
        if not p.actor.is_person and not hcp.allows(p.grant, c, "write"):
            raise hcp.denied(f"not authorized to write {c}")
    expires = None
    if payload.assertion.validUntil:
        expires = parse_datetime(payload.assertion.validUntil)
        if expires is None:
            raise hcp.malformed("assertion.validUntil is ISO-8601")
    claimed = str((payload.record.provenance.assertedBy or {}).get("id") or "")
    basis = _basis(payload.assertion.basis)

    def _create():
        ws = hcp_api._workspace_for_write(p, workspace)
        fact = hcp.add_entry(
            person=p.person, workspace=ws, category=cats[0], statement=payload.content.value,
            declaration=hcp.DECLARATION[decl], actor=p.actor, confidence=payload.assertion.confidence,
            dimension=payload.classification.dimension, source=payload.record.provenance.source,
            source_turn=p.turn, by_user=hcp_api._by_user(request), expires_at=expires,
            user_verified=p.actor.is_person, project=hcp.project_of(p.turn, ws) if ws is not None else None,
            captured_by_override=hcp_api._claimed_capture(p, claimed),
            permitted_purposes=payload.assertion.permittedPurposes, grant=p.grant)
        updates = {}
        if len(cats) > 1:
            updates["extra_categories"] = cats[1:]
        if basis:
            updates["basis_detail"] = basis
        if updates:
            hcp.PersonFact.objects.filter(pk=fact.pk).update(**updates)
            fact.refresh_from_db()
        for r in payload.record.relationships or []:
            entry, source = _target(p, r.target)
            if source is not None:
                if r.type != "derivedFrom":
                    raise hcp.malformed("only derivedFrom may name a source")
                hcp.relationships.assert_derived_from(fact, [{"source": source}], actor=p.actor, grant=p.grant)
            elif r.type == "derivedFrom":
                hcp.relationships.assert_derived_from(fact, [{"entry": entry.entry_id}], actor=p.actor, grant=p.grant)
            else:
                hcp.assert_relationship(fact, r.type, entry, actor=p.actor)
        fact.refresh_from_db()
        return _ok(_render_one(p, fact), status=201)

    def run():
        with transaction.atomic():           # a refusal above leaves no half-written entry
            return _create()
    return hcp_api._idempotent(request, body, run)


class HcpV2RevisionIn(_Body):
    expectedVersion: int
    content: HcpV2ContentIn
    reason: str = Field(description="Why. Recorded in the audit log.")
    model: str | None = Field(default=None, description="The model id behind a model-inferred revision.")


@router.post("/entries/{entry_id}/revisions", operation_id="hcp_v2_reviseEntry", summary="HCP v2: revise an entry")
@v2_route
@gate("self", "hcp-grant", kind="person-data")
def hcp_v2_revise_entry(request: HttpRequest, entry_id: str, payload: HcpV2RevisionIn, turn: str | None = None):
    """Revise (HCP v2 5.2.4): a new version of the same entry, replacing its content whole; a stale
    `expectedVersion` is a `version-conflict` and changes nothing. A client may not revise what the person
    rejected (3.8); the new version starts with no confirmations."""
    p = hcp_api._principal(request, turn, hcp.RECORD)
    if payload.content.profile != v2.GENERAL_CONTEXT:
        raise hcp.malformed(f"content.profile is {v2.GENERAL_CONTEXT}")

    def run():
        fact = _by_name(p, entry_id, "write")
        if not p.actor.is_person and not hcp.within_grant(fact, p.grant, "write"):
            raise hcp.denied()
        if fact.version != payload.expectedVersion:
            raise hcp.HcpError("version-conflict", f"the entry is at version {fact.version}")
        new = hcp.update_entry(fact, statement=payload.content.value, reason=payload.reason, actor=p.actor,
                               by_user=hcp_api._by_user(request), source_turn=p.turn, model=payload.model)
        return _ok(_render_one(p, new))
    return hcp_api._idempotent(request, {"entry": entry_id, **payload.model_dump()}, run)


@router.delete("/entries/{entry_id}", operation_id="hcp_v2_withdrawEntry", summary="HCP v2: withdraw an entry")
@v2_route
@gate("self", "hcp-grant", kind="person-data")  # hardDelete: the person only (governance)
def hcp_v2_withdraw_entry(request: HttpRequest, entry_id: str, reason: str = "", hardDelete: bool = False,
                          turn: str | None = None):
    """Withdraw (HCP v2 5.2.5): the entry is hidden from clients at once and still held and visible to
    you. `hardDelete=true` removes it entirely and is the person's alone, over HTTP only (5.4.2)."""
    p = hcp_api._principal(request, turn, hcp.RECORD)
    fact = _by_name(p, entry_id, "write")
    if not p.actor.is_person and not hcp.within_grant(fact, p.grant, "write"):
        raise hcp.denied()
    if hardDelete and not p.actor.is_person:
        raise hcp.denied("only the person may hard-delete")
    hcp.delete_entry(fact, actor=p.actor, reason=reason or "withdrawn", hard=hardDelete,
                     by_user=hcp_api._by_user(request))
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


# --- grants (5.2.6, 5.2.7, 6.1.5, 6.2) -----------------------------------------------------------


def grant_v2(grant) -> dict:
    """The grant as 6.1.5 records it: what it authorizes (`access`), the purposes approved, what it is
    narrowed to, and the client by a name the person can recognize."""
    scopes = grant.scopes or []
    actions = [a for a in ("read", "write") if hcp.categories_for(grant, a)]
    cats = sorted({c for a in actions for c in hcp.categories_for(grant, a)})
    sources = sorted({s for r in grant.restrictions or [] for s in (r.get("narrowedTo") or {}).get("sources", [])})
    detail = {"type": "https://hcp.example/spec/authorization/context-access", "actions": actions,
              "categories": cats, "purposes": hcp.purposes.describe(hcp.purposes.of_grant(grant))}
    if sources:
        detail["narrowedTo"] = {"sources": sources}
    ident = hcp.disclosure._client_of(grant)
    client = {"id": ident["id"], "name": ident["name"], "operator": ident["operator"]}
    if grant.hcp_client_id:
        if grant.hcp_client.legal_retention:
            client["legalRetention"] = dict(grant.hcp_client.legal_retention)
        if grant.hcp_client.claims:
            client["claims"] = list(grant.hcp_client.claims)
    return {"grantId": hcp.entry_urn(grant.grant_id), "subject": {"id": hcp.subject_id(grant.person)},
            "authorizingPrincipal": {"id": hcp.subject_id(grant.person)}, "client": client, "access": [detail],
            "scopes": list(scopes), "grantType": grant.grant_type, "issuedAt": grant.issued_at.isoformat(),
            "expiresAt": grant.expires_at.isoformat() if grant.expires_at else None, "status": grant.status}


@router.get("/grants", operation_id="hcp_v2_listGrants", summary="HCP v2: grants")
@v2_route
@gate("self", "hcp-grant")  # a client sees only the grant it acts under (5.2.6)
def hcp_v2_list_grants(request: HttpRequest, status: str = "active", turn: str | None = None):
    """The person (a management client): every grant, `status` = active | revoked | expired | all.
    A client: only the grant it acts under (5.2.6); a grant is never disclosed to another client."""
    p = hcp_api._principal(request, turn, hcp.ANY)
    if not p.actor.is_person:
        return _ok({"grants": [grant_v2(p.grant)]})
    qs = hcp.PersonGrant.objects.filter(person=p.person).select_related("agent", "hcp_client")
    for g in qs.filter(status="active", expires_at__isnull=False):
        hcp._expire_if_due(g)
    if status != "all":
        qs = qs.filter(status=status)
    return _ok({"grants": [grant_v2(g) for g in qs.order_by("-issued_at")]})


@router.delete("/grants/{grant_id}", operation_id="hcp_v2_revokeGrant", summary="HCP v2: revoke or relinquish a grant")
@v2_route
@gate("self", "hcp-grant", setting=True)  # the person revokes any; a client may only give up its own
def hcp_v2_revoke_grant(request: HttpRequest, grant_id: str, turn: str | None = None):
    """The person revokes any grant of theirs (6.2.1); a client may give up the grant it acts under
    (5.2.7) and no other. Never rate limited. Every request after it fails, whatever token it carries."""
    tok = getattr(request, "hcp_access", None)
    try:
        gid = uuid.UUID(grant_id.split(":")[-1])
    except ValueError:
        raise hcp.denied() from None
    if tok is not None:
        grant, actor = hcp._expire_if_due(tok.grant), hcp_oauth.client_actor(tok.client)
        if grant.grant_id != gid:
            raise hcp.denied()
    elif hcp_api.people.agent_of_login(request.user) is not None or hcp_api.kinds.speaks_for_a_turn(request):
        p = hcp_api._principal(request, turn, hcp.ANY)
        if p.grant is None or p.grant.grant_id != gid:
            raise hcp.denied()
        grant, actor = p.grant, p.actor
    else:
        p = hcp_api._person_principal(request)
        grant = hcp.PersonGrant.objects.filter(grant_id=gid, person=p.person).first()
        if grant is None:
            raise hcp.denied()
        actor = p.actor
    hcp.revoke_grant(grant, actor=actor)
    resp = HttpResponse(status=204)
    resp["HCP-Version"] = v2.HCP_VERSION_V2
    return resp


# --- the person's own: audit, export, disclosure summary (8.3, 5.4.1, 8.6) -------------------------


@router.get("/audit", operation_id="hcp_v2_listAudit", summary="HCP v2 governance: my audit log")
@v2_route
@gate("self")
def hcp_v2_list_audit(request: HttpRequest, cursor: str | None = None, limit: int = hcp_api.AUDIT_PAGE_DEFAULT):
    """Every read and change of your entries in HCP v2's event shape (8.2), newest first, with an opaque
    cursor (9.5). Yours only; no agent can read it."""
    p = hcp_api._person_principal(request)
    qs = hcp.PersonAuditEvent.objects.filter(person=p.person).order_by("-pk")
    if cursor:
        qs = qs.filter(pk__lt=hcp_api._decode_cursor(cursor))
    limit = max(1, min(int(limit or hcp_api.AUDIT_PAGE_DEFAULT), hcp_api.AUDIT_PAGE_MAX))
    rows = list(qs[:limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return _ok({"events": [v2.event_v2(e) for e in rows],
                "nextCursor": hcp_api._encode_cursor(rows[-1].pk) if more and rows else None})


@router.get("/export", operation_id="hcp_v2_export", summary="HCP v2 governance: export everything held about me")
@v2_route
@gate("self")
def hcp_v2_export(request: HttpRequest, include: str = "", exclude: str = ""):
    """Every entry (current versions, withdrawn ones included unless `exclude=deleted`), your standing rules
    and directives, and, with `include=audit`, the log (5.4.1). The person only."""
    p = hcp_api._person_principal(request)
    inc = {s.strip() for s in include.split(",") if s.strip()}
    exc = {s.strip() for s in exclude.split(",") if s.strip()}
    if inc - {"audit", "versions"} or exc - {"deleted"}:
        raise hcp.malformed("include is audit,versions; exclude is deleted")
    entries = hcp.current_versions(p.person).select_related("person", "workspace", "project")
    if "deleted" in exc:
        entries = entries.exclude(status=hcp.PersonFact.DELETED)
    out = {"@context": v2.CONTEXT_V2, "subject": hcp.subject_id(p.person),
           "entries": [v2.to_v2_entry(f) for f in entries.order_by("created_at")],
           "rules": [hcp.rules.rule_dict(r) for r in p.person.standing_rules.all()],
           "directives": [hcp.rules.directive_dict(d) for d in p.person.directives.all()]}
    if "versions" in inc:
        ids = [f.entry_id for f in entries]
        out["versions"] = [v2.to_v2_entry(f) for f in hcp.PersonFact.objects.filter(
            entry_id__in=ids, superseded_at__isnull=False).order_by("entry_id", "version")]
    if "audit" in inc:
        out["audit"] = [v2.event_v2(e) for e in hcp.PersonAuditEvent.objects.filter(person=p.person).order_by("pk")]
    hcp.audit(p.person, "preference.exported", actor=p.actor,
              detail=f"include={','.join(sorted(inc)) or '-'}; exclude={','.join(sorted(exc)) or '-'}")
    return _ok(out)


@router.get("/disclosures", operation_id="hcp_v2_disclosures", summary="HCP v2 governance: what each client received")
@v2_route
@gate("self")
def hcp_v2_disclosures(request: HttpRequest, client: str | None = None, category: str | None = None,
                       since: str | None = None, until: str | None = None):
    """The disclosure summary (HCP v2 8.6): per client, which grants, categories, purposes, how many
    distinct entries and retrievals, over what period, until when the copies stay usable, and whether a
    revocation reached it. Computed from your audit log; it cannot show what a client did with a copy."""
    p = hcp_api._person_principal(request)
    bounds = {}
    for name, raw in (("since", since), ("until", until)):
        if raw:
            when = parse_datetime(raw.strip().replace(" ", "+"))
            if when is None:
                raise hcp.malformed(f"{raw!r} is not ISO-8601")
            bounds[name] = when
    return _ok({"clients": hcp.disclosure.summary(p.person, client=client, category=category, **bounds)})
