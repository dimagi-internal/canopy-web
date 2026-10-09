"""/api/hcp — the Human Context Protocol v1 (draft 4) over HTTP, and over MCP.

The base URL is `/api/hcp`; the discovery document is at
`/api/hcp/.well-known/hcp-configuration`. Every route is also an MCP tool (the
REST API is served as MCP — `apps/mcp/api_tools.py`), and the four preference
operations carry the spec's own tool names: `hcp_searchPreferences`,
`hcp_addPreference`, `hcp_updatePreference`, `hcp_deletePreference` (3.2).
The person-only routes — audit, grants, export — are kept off MCP, as
Appendix B says.

**Whose instance, and who is asking.** Each person has one HCP instance (their
`Person`). Three callers reach it:

* **the person themself** (session or their own PAT) — their own entries, all
  categories canopy holds, unredacted;
* **an agent** (its own login's PAT) — only the person who STARTED the turn it
  names (`turn`), and only under that client's grant: the caller-only rule
  is the subject resolution, not a convention;
* **a confined session's caller token** — the same, the turn being the token's.

Errors are RFC 9457 problems with the HCP registered types (3.4.4); a missing
entry and an unauthorized one are the same 403 (3.3.6).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import struct
import uuid
from dataclasses import dataclass
from functools import wraps

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from ninja import Router
from pydantic import BaseModel, ConfigDict, Field

from apps.api.auth import session_auth

from . import hcp, people
from . import services as contact_services
from .models import HcpIdempotencyKey, Person, PersonAuditEvent, PersonFact, PersonGrant

router = Router(auth=session_auth, tags=["hcp"])

LD_JSON = "application/ld+json"
IDEMPOTENCY_TTL_HOURS = 24
AUDIT_PAGE_DEFAULT, AUDIT_PAGE_MAX = 50, 200


# --- responses -------------------------------------------------------------------


def _ok(body: dict, status: int = 200) -> JsonResponse:
    body = {**body, "hcp_version": hcp.HCP_VERSION}             # 3.4.1
    return JsonResponse(body, status=status, content_type=LD_JSON)


def _problem(request: HttpRequest, err: hcp.HcpError) -> HttpResponse:
    body = {"type": hcp.PROBLEM_BASE + err.problem, "title": err.problem.replace("-", " "),
            "status": err.status, "detail": err.detail or err.problem,
            "instance": request.path, "hcp_version": hcp.HCP_VERSION}
    resp = HttpResponse(json.dumps(body), status=err.status, content_type="application/problem+json")
    if err.status == 429:
        resp["Retry-After"] = "60"
    return resp


def hcp_route(view):
    """Version check (3.4.1) and HcpError → problem+json, on every route."""
    @wraps(view)
    def wrapper(request: HttpRequest, *args, **kwargs):
        try:
            asked = (request.headers.get("HCP-Version") or "").strip()
            if asked and asked != hcp.HCP_VERSION:
                raise hcp.HcpError("unsupported-version", f"this instance serves {hcp.HCP_VERSION}")
            return view(request, *args, **kwargs)
        except hcp.HcpError as err:
            return _problem(request, err)
    return wrapper


# --- who is asking, about whom ------------------------------------------------------


@dataclass
class Principal:
    person: Person
    actor: hcp.Actor
    grant: PersonGrant | None       # None for the person themself
    workspace_slug: str | None      # an agent is confined to its workspace
    turn: object = None


def _turn_agent(turn):
    if turn.agent_id:
        return turn.agent
    if turn.chat_session_id and turn.chat_session.agent_id:
        return turn.chat_session.agent
    return None


def _principal(request: HttpRequest, turn_id: str | None, need: str = hcp.ANY) -> Principal:
    """`need` is the person's switch an AGENT must find on: `hcp.USE` for a read,
    `hcp.RECORD` for a write (`Person.hcp_use` / `hcp_record`). The person
    themself is never gated by it."""
    from apps.harness.models import Turn

    user = request.user
    if not getattr(user, "is_authenticated", False):
        raise hcp.HcpError("invalid-token", "a bearer token is required")
    caller_turn = None
    if getattr(request, "auth_method", "") == "caller_token":
        cred = getattr(request, "auth_credential", None) or {}
        caller_turn = str(cred.get("turn_id") or "")
    agent = people.agent_of_login(user)
    if agent is None and not caller_turn:
        person = contact_services.person_for(user=user)
        if person is None:
            raise hcp.denied("no HCP instance for this account")
        return Principal(person=person, actor=hcp.user_actor(user), grant=None, workspace_slug=None)
    # An agent (its own login, or a confined session acting for its caller):
    # the subject is the human who started the turn, and nobody else.
    tid = caller_turn or (turn_id or request.headers.get("HCP-Turn") or "").strip()
    if not tid:
        raise hcp.malformed("an agent names the turn it is serving: turn=<turn id>")
    try:
        tid_uuid = uuid.UUID(tid)
    except ValueError:
        raise hcp.denied() from None
    turn = (Turn.objects.select_related("agent", "chat_session__agent", "initiator_user",
                                        "initiator_contact__app")
            .filter(pk=tid_uuid).first())
    if turn is None:
        raise hcp.denied()
    serving = _turn_agent(turn)
    if serving is None or (agent is not None and serving.pk != agent.pk):
        raise hcp.denied("that turn is not this agent's")
    if caller_turn and caller_turn != str(turn.pk):
        raise hcp.denied()
    person = people.initiator_person(turn)
    if person is None:
        raise hcp.denied("no person started that turn")
    # The person's own switches, checked before any grant is presumed: a person who
    # has not allowed this kind of operation is not served, and no grant is issued.
    hcp.require(person, need)
    channel, host = hcp.client_of_turn(turn)
    grant = hcp.grant_for(person, agent=serving, workspace_slug=serving.workspace_id,
                          channel=channel, host=host)
    if grant is None:
        raise hcp.denied("the person has revoked this client's access")
    return Principal(person=person, actor=hcp.agent_actor(serving), grant=grant,
                     workspace_slug=serving.workspace_id, turn=turn)


def _person_principal(request: HttpRequest) -> Principal:
    """The audit log, revocation and export are the PERSON's (4.3.3, 4.2.1,
    3.3.5): an agent — its own login or a caller token — is refused outright,
    whatever turn it names."""
    if (people.agent_of_login(request.user) is not None
            or getattr(request, "auth_method", "") == "caller_token"):
        raise hcp.denied("only the person may do this")
    return _principal(request, None)


def _entry(p: Principal, entry_id: str, action: str) -> PersonFact:
    """The entry's current version, if this principal may `action` it; else the
    same 403 as a missing one."""
    fact = hcp.current(hcp.parse_entry_id(entry_id))
    if fact is None or fact.person_id != p.person.pk:
        raise hcp.denied()
    if p.actor.is_person:
        return fact
    if not hcp.within_grant(fact, p.grant, action):
        raise hcp.denied()
    if fact.retracted_at is not None or fact.status in (PersonFact.CONFLICTED, PersonFact.DEPRECATED):
        raise hcp.denied()                                       # 2.6.2: withheld from agents
    if fact.expires_at is not None and fact.expires_at <= timezone.now():
        raise hcp.denied()
    return fact


# --- idempotency (3.4.3) ---------------------------------------------------------


def _idempotent(request: HttpRequest, body: dict, run):
    key = (request.headers.get("Idempotency-Key") or "").strip()[:200]
    if not key:
        return run()
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    cutoff = timezone.now() - dt.timedelta(hours=IDEMPOTENCY_TTL_HOURS)
    HcpIdempotencyKey.objects.filter(user=request.user, created_at__lt=cutoff).delete()
    seen = HcpIdempotencyKey.objects.filter(user=request.user, key=key).first()
    if seen is not None:
        if seen.body_hash != digest or seen.method != request.method or seen.path != request.path:
            raise hcp.HcpError("idempotency-conflict", "this Idempotency-Key was used with a different request")
        return JsonResponse(seen.response, status=seen.status, content_type=LD_JSON)
    with transaction.atomic():
        resp = run()
        if 200 <= resp.status_code < 300:
            HcpIdempotencyKey.objects.create(user=request.user, key=key, method=request.method,
                                             path=request.path[:300], body_hash=digest,
                                             status=resp.status_code,
                                             response=json.loads(resp.content))
    return resp


# --- bodies --------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class HcpSearchIn(_Body):
    query: str = Field(description="Natural language query describing the context or task.")
    categories: list[str] = Field(description="Category slugs to search. Must be within authorized scopes.")
    purpose: str = Field(description="Plain language statement of purpose. Logged to the person's audit trail.")
    maxEntries: int = Field(default=hcp.DEFAULT_MAX_ENTRIES, description="Maximum entries to return. Default 5, maximum 20.")
    responseDetail: str = Field(default="full", description='"full" or "minimal" (id, category, dimension, value).')


class HcpAddIn(_Body):
    """The MCP binding of addPreference (3.2.2): Tier 1 only."""

    category: str = Field(description="Category slug: work_context, general_preferences, goals_and_constraints, coordination_context, or hcp-custom:<name>.")
    dimension: str | None = Field(default=None, description="Optional. The preference dimension within the category; used for conflict detection.")
    preference: str = Field(description="Natural language preference statement (one sentence, ≤500 characters).")
    declarationType: str = Field(description="user-declared (the person said it) or model-inferred (you concluded it).")
    confidence: str | None = Field(default=None, description="high | medium | low — required for model-inferred, omitted otherwise.")
    sourceContext: str = Field(description="Conversation or document id this was derived from (e.g. turn:<id>).")
    model: str | None = Field(default=None, description="The model id that produced a model-inferred entry, e.g. claude-opus-5-5")


class HcpEntryIn(_Body):
    """POST /v1/preferences (3.3.2): an HCPEntry with server-assigned fields omitted."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)
    claim: dict
    record: dict
    credentialType: str = "NLPreference"


class HcpUpdateIn(_Body):
    updatedPreference: str = Field(description="Revised preference statement.")
    reason: str = Field(description="Reason for the update. Logged to the person's audit trail.")
    category: str | None = Field(default=None, description="The person only: move the entry to another category.")
    dimension: str | None = Field(default=None, description="Optional: revise the dimension.")
    model: str | None = Field(default=None, description="The model id that produced a model-inferred entry, e.g. claude-opus-5-5")


def _workspace_for_write(p: Principal, explicit: str | None):
    from apps.workspaces import services as wsvc
    from apps.workspaces.models import Workspace

    slug = p.workspace_slug if not p.actor.is_person else (explicit or "").strip()
    if not slug:
        raise hcp.malformed("say which workspace the entry belongs to: workspace=<slug>")
    if p.actor.is_person and not (p.person.user_id and wsvc.is_member(p.person.user, slug)):
        raise hcp.denied("not a workspace you are a member of")
    ws = Workspace.objects.filter(pk=slug).first()
    if ws is None:
        raise hcp.denied()
    return ws


def _search_workspace(p: Principal, explicit: str | None) -> list[str]:
    if not p.actor.is_person:
        return [p.workspace_slug]
    if explicit:
        return [explicit.strip()]
    return list(hcp.current_versions(p.person).values_list("workspace_id", flat=True).distinct())


def _render(fact: PersonFact, p: Principal, detail: str = "full") -> dict:
    if detail == "minimal":
        return hcp.to_minimal(fact)
    return hcp.to_entry(fact, redact=not p.actor.is_person)


# --- discovery -------------------------------------------------------------------


@router.get("/.well-known/hcp-configuration", auth=None, operation_id="hcp_configuration",
            summary="HCP discovery document")
def hcp_configuration(request: HttpRequest):
    """Appendix C: what this HCP instance supports."""
    base = request.build_absolute_uri("/api/hcp").rstrip("/")
    return _ok(hcp.discovery(base))


# --- the four operations ----------------------------------------------------------


@router.post("/v1/preferences/search", operation_id="hcp_searchPreferences",
             summary="HCP: recall what is relevant about the person you are serving")
@hcp_route
def hcp_search_preferences(request: HttpRequest, payload: HcpSearchIn, turn: str | None = None,
                           workspace: str | None = None):
    """Retrieve minimal, relevant preference entries from the person's HCP
    instance for a stated purpose (HCP 3.2.1). An agent names the turn it is
    serving (`turn`) and gets only that turn's person, under its grant; at most
    `maxEntries` (≤ 20), ordered by relevance, never padded; provenance source
    and capturedBy are redacted. Every call is on the person's audit log."""
    p = _principal(request, turn, hcp.USE)
    if payload.responseDetail not in ("full", "minimal"):
        raise hcp.malformed('responseDetail is "full" or "minimal"')
    hits, searched, retrieval = [], [], None
    for slug in _search_workspace(p, workspace):
        found, searched, retrieval = hcp.search(
            p.person, workspace_slug=slug, query=payload.query, categories=payload.categories,
            purpose=payload.purpose, max_entries=payload.maxEntries, grant=p.grant,
            actor=p.actor, turn=p.turn)
        hits.extend(found)
    hits = hits[:min(payload.maxEntries, hcp.MAX_ENTRIES)]
    return _ok({"entries": [_render(f, p, payload.responseDetail) for f in hits],
                "retrievalId": f"urn:uuid:{retrieval or uuid.uuid4()}",
                "minimization": hcp.minimization(p.actor),
                "categoriesSearched": searched or payload.categories,
                "timestamp": timezone.now().isoformat()})


@router.post("/v1/preferences/add", operation_id="hcp_addPreference",
             summary="HCP: remember something about the person you are serving")
@hcp_route
def hcp_add_preference(request: HttpRequest, payload: HcpAddIn, turn: str | None = None,
                       workspace: str | None = None):
    """Add a new preference entry (HCP 3.2.2) — Tier 1, about the person who
    started `turn`, in a category your grant may write. `model-inferred` needs
    a `confidence`; an inference that contradicts something the person declared
    on the same dimension is quarantined until they resolve it."""
    p = _principal(request, turn, hcp.RECORD)
    body = payload.model_dump()

    def run():
        if payload.declarationType not in ("user-declared", "model-inferred"):
            raise hcp.malformed("declarationType is user-declared or model-inferred")
        if not p.actor.is_person and not hcp.allows(p.grant, payload.category, "write"):
            raise hcp.denied(f"not authorized to write {payload.category}")
        captured = hcp.captured_by_model(
            p.actor, inferred=payload.declarationType == "model-inferred", model=payload.model)
        ws = _workspace_for_write(p, workspace)
        fact = hcp.add_entry(
            person=p.person, workspace=ws, category=payload.category, statement=payload.preference,
            declaration=payload.declarationType, actor=p.actor, confidence=payload.confidence,
            dimension=payload.dimension, source=payload.sourceContext, source_turn=p.turn,
            by_user=request.user, user_verified=p.actor.is_person,
            captured_by_override="" if captured == p.actor.id else captured)
        return _ok({"entry": _render(fact, p)}, status=201)
    return _idempotent(request, body, run)


@router.post("/v1/preferences", operation_id="hcp_createEntry",
             summary="HCP: add a full HCPEntry (REST, 3.3.2)")
@hcp_route
def hcp_create_entry(request: HttpRequest, payload: HcpEntryIn, turn: str | None = None,
                     workspace: str | None = None):
    """POST /v1/preferences: an HCPEntry (grouped or flat form) with the
    server-assigned fields omitted. Tier 1 only; `issuer-attested` allowed here."""
    p = _principal(request, turn, hcp.RECORD)
    if payload.credentialType != "NLPreference":
        raise hcp.malformed("this instance holds Tier 1 (NLPreference) entries only")
    claim, record = payload.claim or {}, payload.record or {}
    subject = claim.get("subject") or {}
    prov = record.get("provenance") or {}
    category = record.get("category") or ""
    if not p.actor.is_person and not hcp.allows(p.grant, category, "write"):
        raise hcp.denied(f"not authorized to write {category}")
    if record.get("declarationType") == "issuer-attested" and p.actor.is_person:
        raise hcp.malformed("an entry you assert about yourself is user-declared")
    expires = None
    if claim.get("expirationDate"):
        expires = parse_datetime(str(claim["expirationDate"]))
        if expires is None:
            raise hcp.malformed("claim.expirationDate is not ISO-8601")
    meta = record.get("metadata") or {}
    if not isinstance(meta, dict):
        raise hcp.malformed("record.metadata is an object")

    def run():
        ws = _workspace_for_write(p, workspace)
        fact = hcp.add_entry(
            person=p.person, workspace=ws, category=category,
            statement=str(subject.get("preference") or ""),
            declaration=str(record.get("declarationType") or ""), actor=p.actor,
            confidence=record.get("confidence"), dimension=record.get("dimension"),
            source=str(prov.get("source") or ""), source_turn=p.turn, by_user=request.user,
            expires_at=expires, relationship=str(subject.get("relationship") or ""),
            metadata={k: v for k, v in meta.items() if not str(k).startswith("canopy:")},
            user_verified=bool(prov.get("userVerified")) and p.actor.is_person,
            captured_by_override=str(prov.get("capturedBy") or ""))
        return _ok({"entry": _render(fact, p)}, status=201)
    return _idempotent(request, payload.model_dump(), run)


@router.get("/v1/preferences/{entry_id}", operation_id="hcp_getPreference",
            summary="HCP: one entry, by id")
@hcp_route
def hcp_get_preference(request: HttpRequest, entry_id: str, purpose: str = "",
                       version: int | None = None, turn: str | None = None):
    """GET /v1/preferences/{entryId} (3.3.6). `purpose` is required and logged.
    `version` returns an earlier version. 403 for a missing entry too."""
    p = _principal(request, turn, hcp.USE)
    fact = _entry(p, entry_id, "read")
    if version is not None:
        if not p.actor.is_person:
            raise hcp.denied("earlier versions are the person's")
        fact = hcp.version_of(fact.entry_id, version) or None
        if fact is None:
            raise hcp.denied()
    hcp.read_one(fact, actor=p.actor, grant=p.grant, purpose=purpose, turn=p.turn)
    return _ok({"entry": _render(fact, p), "retrievalId": f"urn:uuid:{uuid.uuid4()}",
                "minimization": hcp.minimization(p.actor), "timestamp": timezone.now().isoformat()})


@router.put("/v1/preferences/{entry_id}", operation_id="hcp_updatePreference",
            summary="HCP: correct an entry, preserving its history")
@hcp_route
def hcp_update_preference(request: HttpRequest, entry_id: str, payload: HcpUpdateIn,
                          turn: str | None = None):
    """Update an existing entry (HCP 3.2.3): a new version, same id, version + 1.
    When the PERSON updates a quarantined inference they accept it — it becomes
    theirs and the entry it contradicted is deprecated."""
    p = _principal(request, turn, hcp.RECORD)

    def run():
        fact = _entry(p, entry_id, "write")
        new = hcp.update_entry(fact, statement=payload.updatedPreference, reason=payload.reason,
                               actor=p.actor, category=payload.category, dimension=payload.dimension,
                               by_user=request.user, source_turn=p.turn, model=payload.model)
        return _ok({"entry": _render(new, p)})
    return _idempotent(request, {"entry": entry_id, **payload.model_dump()}, run)


@router.delete("/v1/preferences/{entry_id}", operation_id="hcp_deletePreference",
               summary="HCP: forget an entry")
@hcp_route
def hcp_delete_preference(request: HttpRequest, entry_id: str, reason: str = "",
                          hardDelete: bool = False, turn: str | None = None):
    """Mark an entry deleted (HCP 3.2.4). `hardDelete=true` — the person only —
    removes every version permanently (GDPR/CCPA)."""
    p = _principal(request, turn, hcp.RECORD)
    fact = _entry(p, entry_id, "write")
    hcp.delete_entry(fact, actor=p.actor, reason=reason, hard=hardDelete, by_user=request.user)
    return _ok({"entryId": hcp.entry_urn(fact.entry_id), "status": "deleted",
                "hardDeleted": bool(hardDelete), "timestamp": timezone.now().isoformat()})


# --- the person's own: audit, grants, export ----------------------------------------

_CURSOR_SALT = b"hcp-audit-cursor-v1"


def _cursor_key() -> bytes:
    return hmac.new(settings.SECRET_KEY.encode(), _CURSOR_SALT, hashlib.sha256).digest()


def _encode_cursor(pk: int) -> str:
    """Opaque (3.4.2): the position is encrypted, so a client cannot read the
    size of the log from it."""
    nonce = os.urandom(8)
    pad = hmac.new(_cursor_key(), nonce, hashlib.sha256).digest()[:8]
    body = bytes(a ^ b for a, b in zip(struct.pack(">Q", pk), pad))
    mac = hmac.new(_cursor_key(), nonce + body, hashlib.sha256).digest()[:8]
    return base64.urlsafe_b64encode(nonce + body + mac).decode().rstrip("=")


def _decode_cursor(token: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        nonce, body, mac = raw[:8], raw[8:16], raw[16:24]
        if not hmac.compare_digest(mac, hmac.new(_cursor_key(), nonce + body, hashlib.sha256).digest()[:8]):
            raise ValueError
        pad = hmac.new(_cursor_key(), nonce, hashlib.sha256).digest()[:8]
        return struct.unpack(">Q", bytes(a ^ b for a, b in zip(body, pad)))[0]
    except Exception:  # noqa: BLE001
        raise hcp.malformed("cursor is not valid") from None


def _event_dict(e: PersonAuditEvent) -> dict:
    return {"eventId": hcp.entry_urn(e.event_id), "eventType": e.event_type,
            "timestamp": e.timestamp.isoformat(), "actorId": e.actor_id, "actorType": e.actor_type,
            "entryId": hcp.entry_urn(e.entry_id) if e.entry_id else None,
            "relatedEntryId": hcp.entry_urn(e.related_entry_id) if e.related_entry_id else None,
            "category": e.category or None, "purpose": e.purpose or None,
            "grantId": hcp.entry_urn(e.grant_id) if e.grant_id else None,
            "detail": e.detail or None}


@router.get("/v1/audit", operation_id="hcp_listAudit", summary="HCP: my audit log")
@hcp_route
def hcp_list_audit(request: HttpRequest, cursor: str | None = None, limit: int = AUDIT_PAGE_DEFAULT,
                   actor: str | None = None, category: str | None = None,
                   eventType: str | None = None, since: str | None = None, until: str | None = None):
    """Every read and change of your entries, newest first (HCP 4.3). Yours only;
    no agent can read it. Filter by actor, category, event type, date range."""
    p = _person_principal(request)
    qs = PersonAuditEvent.objects.filter(person=p.person).order_by("-pk")
    if actor:
        qs = qs.filter(actor_id=actor)
    if category:
        qs = qs.filter(category=category)
    if eventType:
        qs = qs.filter(event_type=eventType)
    for bound, op in ((since, "timestamp__gte"), (until, "timestamp__lte")):
        if bound:
            when = parse_datetime(bound.strip().replace(" ", "+"))
            if when is None:
                raise hcp.malformed(f"{bound!r} is not ISO-8601")
            qs = qs.filter(**{op: when})
    if cursor:
        qs = qs.filter(pk__lt=_decode_cursor(cursor))
    limit = max(1, min(int(limit or AUDIT_PAGE_DEFAULT), AUDIT_PAGE_MAX))
    rows = list(qs[:limit + 1])
    more = len(rows) > limit
    rows = rows[:limit]
    return _ok({"events": [_event_dict(e) for e in rows],
                "nextCursor": _encode_cursor(rows[-1].pk) if more and rows else None})


@router.get("/v1/grants", operation_id="hcp_listGrants", summary="HCP: who may read what about me")
@hcp_route
def hcp_list_grants(request: HttpRequest, status: str = "active", turn: str | None = None):
    """The person: every grant (`status` = active | revoked | expired | all).
    An agent: only the grant it is reading under (4.1.5)."""
    p = _principal(request, turn, hcp.ANY)
    if not p.actor.is_person:
        return _ok({"grants": [hcp.grant_dict(p.grant)]})
    qs = PersonGrant.objects.filter(person=p.person).select_related("agent").order_by("-issued_at")
    for g in qs.filter(status=PersonGrant.ACTIVE, expires_at__isnull=False):
        hcp._expire_if_due(g)
    if status != "all":
        qs = qs.filter(status=status)
    return _ok({"grants": [hcp.grant_dict(g) for g in qs]})


@router.delete("/v1/grants/{grant_id}", operation_id="hcp_revokeGrant", summary="HCP: revoke a grant")
@hcp_route
def hcp_revoke_grant(request: HttpRequest, grant_id: str):
    """Revoke one client's access, immediately (HCP 4.2). The person only; never
    rate-limited. canopy will not presume it again — re-allowing is yours."""
    p = _person_principal(request)
    try:
        gid = uuid.UUID(grant_id.split(":")[-1])
    except ValueError:
        raise hcp.denied() from None
    grant = PersonGrant.objects.filter(grant_id=gid, person=p.person).first()
    if grant is None:
        raise hcp.denied()
    grant = hcp.revoke_grant(grant, actor=p.actor)
    return _ok({"grant": hcp.grant_dict(grant)})


@router.get("/v1/export", operation_id="hcp_export", summary="HCP: export everything held about me")
@hcp_route
def hcp_export(request: HttpRequest, include: str = "", exclude: str = ""):
    """Every entry's current version (soft-deleted ones included unless
    `exclude=deleted`), optionally every version (`include=versions`) and the
    audit log (`include=audit`). The person only (3.3.5)."""
    p = _person_principal(request)
    inc = {s.strip() for s in include.split(",") if s.strip()}
    exc = {s.strip() for s in exclude.split(",") if s.strip()}
    if inc - {"audit", "versions"} or exc - {"deleted"}:
        raise hcp.malformed("include is audit,versions; exclude is deleted")
    entries = hcp.current_versions(p.person).select_related("person", "workspace", "project",
                                                             "asserted_by_agent", "asserted_by_user")
    if "deleted" in exc:
        entries = entries.exclude(status=PersonFact.DELETED)
    out = {"@context": hcp.CONTEXT, "subject": hcp.subject_id(p.person),
           "entries": [hcp.to_entry(f, redact=False) for f in entries.order_by("created_at")]}
    if "versions" in inc:
        ids = [f.entry_id for f in entries]
        out["versions"] = [hcp.to_entry(f, redact=False) for f in
                           PersonFact.objects.filter(entry_id__in=ids, superseded_at__isnull=False)
                           .select_related("person", "workspace", "project").order_by("entry_id", "version")]
    if "audit" in inc:
        out["audit"] = [_event_dict(e) for e in
                        PersonAuditEvent.objects.filter(person=p.person).order_by("pk")]
    hcp.audit(p.person, "preference.exported", actor=p.actor,
              detail=f"include={','.join(sorted(inc)) or '-'}; exclude={','.join(sorted(exc)) or '-'}")
    return _ok(out)
