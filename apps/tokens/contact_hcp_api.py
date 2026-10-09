"""`/api/contact/hcp/…` — a CONTACT's own HCP, from a site canopy trusts for email.

Jonathan, 2026-10-09: "allow contacts to start building up HCP interactions if they
opt in from a trusted system like labs where we do trust their e-mail identity."
Design and every judgment call: docs/architecture/hcp-service.md § Contacts.

Two things have to be true at once, and they pull against each other:

* **Identity comes from the site.** A contact has no canopy account; on a site
  canopy trusts for it (`AppCredential.asserts_verified_email`, superuser-set) the
  address the site signed `email_verified: true` for — recorded on the contact
  token, `ContactToken.verified_email` — keys the person (`person_for(email=)`). On
  any other site nothing here answers.
* **The act must not.** The site minted the visitor's token, so anything the token
  alone can do, the site can do — including opting every visitor in. So every
  route here also needs a FRAME PROOF: minted only to a request that carries
  canopy's frame cookie (set by the embed shell document: HttpOnly, partitioned)
  and that the browser itself marks `Sec-Fetch-Site: same-origin`, bound to that
  cookie and this contact, single use, minutes long. The host page's scripts get
  none of the three: they cannot read the cookie, their requests are cross-origin
  (and carry no canopy cookie — CORS never allows credentials, `tokens/cors.py`),
  and they cannot read the frame. What this does NOT stop is a host's own SERVER
  impersonating its visitor with forged headers — a server that does that can
  already sign anything about its visitor, which is exactly why trust is a
  superuser decision per site, not a tenant setting.

The opted-in contact holds the same powers a signed-in person has on /people/me
for themselves — the per-agent session grant and its separate keep act (#1389),
their policy, revoke, export — and nothing more. An address that belongs to a
canopy account is refused: that person signs in and uses /people/me, and a contact
must never change an account's settings.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from .contact_api import contact_auth
from .models import HcpFrameProof
from .views_embed import FRAME_COOKIE

contact_hcp_router = Router(auth=contact_auth, tags=["contact"])

#: How long a proof lives, unspent. A proof is fetched right before the call it
#: authorizes, so this only has to cover one round trip.
PROOF_TTL = timezone.timedelta(minutes=5)
PROOF_HEADER = "X-Canopy-Frame-Proof"


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _own_origin(request: HttpRequest) -> str:
    return f"{request.scheme}://{request.get_host()}"


def _from_canopys_frame(request: HttpRequest) -> str:
    """The frame cookie, if this request came from canopy's own frame — else 403.

    The browser decides both signals: `Sec-Fetch-Site` and the cookie a script
    cannot read. And canopy must not be served on the framing site's own origin
    (the retired /canopy prefix on labs), where the host's scripts would BE the
    frame's origin."""
    cookie = request.COOKIES.get(FRAME_COOKIE) or ""
    if not cookie:
        raise HttpError(403, "open this in canopy's panel")
    if (request.headers.get("Sec-Fetch-Site") or "") != "same-origin":
        raise HttpError(403, "open this in canopy's panel")
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if request.method != "GET" and origin != _own_origin(request):
        raise HttpError(403, "open this in canopy's panel")
    app = request.contact_token.app
    if _own_origin(request) in app.frame_origins():
        raise HttpError(403, "canopy is not isolated from this site here; use canopy directly")
    return cookie


def _spend_proof(request: HttpRequest) -> None:
    cookie = _from_canopys_frame(request)
    raw = request.headers.get(PROOF_HEADER) or ""
    if not raw:
        raise HttpError(403, "open this in canopy's panel")
    with transaction.atomic():
        spent = (HcpFrameProof.objects
                 .filter(proof_hash=_hash(raw), contact=request.contact,
                         cookie_hash=_hash(cookie), used_at__isnull=True,
                         expires_at__gt=timezone.now())
                 .update(used_at=timezone.now()))
    if spent != 1:
        raise HttpError(403, "open this in canopy's panel")


class ProofOut(Schema):
    proof: str
    expires_in: int


@contact_hcp_router.post("/proof", response=ProofOut,
                         summary="A single-use proof that this is canopy's own panel")
def contact_hcp_proof(request: HttpRequest) -> ProofOut:
    """Only canopy's panel can get one: send it as `X-Canopy-Frame-Proof` on the
    next call to `/api/contact/hcp/`. One proof, one call, five minutes."""
    cookie = _from_canopys_frame(request)
    raw = secrets.token_urlsafe(32)
    HcpFrameProof.objects.create(contact=request.contact, proof_hash=_hash(raw),
                                 cookie_hash=_hash(cookie),
                                 expires_at=timezone.now() + PROOF_TTL)
    # Spent and expired proofs are dead weight; sweep this contact's on the way.
    HcpFrameProof.objects.filter(contact=request.contact,
                                 expires_at__lt=timezone.now() - PROOF_TTL).delete()
    return ProofOut(proof=raw, expires_in=int(PROOF_TTL.total_seconds()))


# --- who this contact is, for HCP ------------------------------------------------------


def _eligibility(request: HttpRequest) -> tuple[str, str]:
    """(verified address, "") when this contact may use HCP here, else ("", reason)."""
    from apps.contacts import services as contact_services

    token = request.contact_token
    if not token.app.asserts_verified_email:
        return "", "untrusted_site"
    email = token.verified_email
    if not email:
        return "", "unverified_email"
    if contact_services.user_for_verified_email(email) is not None or _account_holds(email):
        return "", "has_account"
    return email, ""


def _account_holds(email: str) -> bool:
    """Any canopy account at all with this address — verified or not. Conservative
    on purpose: a contact must never act on, or merge into, an account's person."""
    from allauth.account.models import EmailAddress
    from django.contrib.auth import get_user_model

    return (EmailAddress.objects.filter(email__iexact=email).exists()
            or get_user_model().objects.filter(email__iexact=email).exists())


def _person(request: HttpRequest, *, create: bool):
    """The email-keyed person this contact is, once eligible. `create` only on the
    opt-in itself; until then a contact has no HCP person to read."""
    from apps.contacts import services as contact_services
    from apps.contacts.models import Contact, Person

    email, reason = _eligibility(request)
    if not email:
        raise HttpError(403, _REASONS[reason])
    if create:
        person = contact_services.person_for(email=email)
    else:
        person = Person.objects.filter(issuer="", signer="", external_id="", email=email).first()
        if person is None:
            return None
    if person.user_id is not None:
        raise HttpError(403, _REASONS["has_account"])
    contact = request.contact
    if create and contact.person_id != person.pk:
        # The site's own id for this visitor keyed a person nobody else can reach;
        # the trusted address is the identity from now on. The old person is left
        # in place (it held nothing HCP: no contact could grant before this).
        Contact.objects.filter(pk=contact.pk).update(person=person)
        contact.person_id = person.pk
    return person


_REASONS = {
    "untrusted_site": "this site is not trusted for your identity in canopy",
    "unverified_email": "this site did not confirm your email address",
    "has_account": "this address has a canopy account — sign in to canopy to manage this",
}


def _session(request: HttpRequest, session_id: str):
    from apps.canopy_sessions.access import contact_session_q
    from apps.canopy_sessions.models import Session

    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        raise HttpError(404, "Not found") from None
    session = (Session.objects.filter(contact_session_q(request.contact), pk=sid)
               .select_related("agent").first())
    if session is None or session.agent is None:
        raise HttpError(404, "Not found")
    return session


def _hcp_error(exc) -> HttpError:
    return HttpError(exc.status, exc.detail)


# --- what the panel shows ---------------------------------------------------------------


class HcpGrantOut(Schema):
    grant_id: str
    agent: str
    type: str
    features: list[str]
    expires_at: str | None = None


class HcpEntryOut(Schema):
    entry_id: str
    category: str
    statement: str
    status: str


class ContactHcpOut(Schema):
    eligible: bool
    reason: str = ""
    opted_in: bool = False
    email: str = ""
    site: str = ""
    categories: list[str] = []
    session_grant_hours: int = 24
    policy: dict = {}
    session: dict | None = None
    grants: list[HcpGrantOut] = []
    #: Every live entry held about them (not soft-deleted), newest first.
    entries: list[HcpEntryOut] = []


def _state(request: HttpRequest, session=None, person=None) -> ContactHcpOut:
    from apps.contacts import hcp
    from apps.contacts.models import PersonGrant
    from apps.contacts.people_api import session_memory_payload

    email, reason = _eligibility(request)
    out = ContactHcpOut(eligible=bool(email), reason=reason, email=email,
                        site=request.contact_token.app.name,
                        categories=list(hcp.HELD_CATEGORIES),
                        session_grant_hours=int(hcp.SESSION_GRANT_CAP.total_seconds() // 3600))
    if not email:
        return out
    person = person or _person(request, create=False)
    if person is None:
        return out
    out.opted_in = any(getattr(person, f"hcp_{f}_available") for f in hcp.FEATURES)
    out.policy = {f: {"available": bool(getattr(person, f"hcp_{f}_available")),
                      "default": bool(getattr(person, f"hcp_{f}_default"))} for f in hcp.FEATURES}
    if session is not None:
        out.session = session_memory_payload(person, session)
    grants = []
    for g in (PersonGrant.objects.filter(person=person, status=PersonGrant.ACTIVE,
                                         agent__isnull=False)
              .select_related("agent").order_by("-issued_at")):
        g = hcp._expire_if_due(g)
        if g.status != PersonGrant.ACTIVE:
            continue
        grants.append(HcpGrantOut(grant_id=hcp.entry_urn(g.grant_id),
                                  agent=g.agent.name or g.agent.slug, type=g.grant_type,
                                  features=sorted(hcp.features_of(g)),
                                  expires_at=g.expires_at.isoformat() if g.expires_at else None))
    out.grants = grants
    from apps.contacts.models import PersonFact

    out.entries = [HcpEntryOut(entry_id=hcp.entry_urn(f.entry_id), category=f.category,
                               statement=f.statement, status=f.status)
                   for f in hcp.current_versions(person).exclude(status=PersonFact.DELETED)
                   .order_by("-created_at")[:200]]
    return out


@contact_hcp_router.get("/state", response=ContactHcpOut,
                        summary="What canopy may learn about me, from this site")
def contact_hcp_state(request: HttpRequest, session_id: str = "") -> ContactHcpOut:
    """Whether you can use agent memory from this site, whether you have opted in,
    your settings, this conversation's state, and every grant you have given.
    Needs a frame proof."""
    _spend_proof(request)
    session = _session(request, session_id) if session_id else None
    return _state(request, session)


class OptInIn(Schema):
    session_id: str
    #: Also let agents USE what they learn. Off unless you tick it.
    use: bool = False


@contact_hcp_router.post("/opt-in", response=ContactHcpOut,
                         summary="Let canopy learn about me in this conversation")
def contact_hcp_opt_in(request: HttpRequest, payload: OptInIn) -> ContactHcpOut:
    """Your act, in canopy's panel: turns agent memory on for you (learning on by
    default; use only if you ticked it) and lets THIS conversation's agent learn
    — for this conversation only. Keeping it for that agent is a separate choice.
    Needs a frame proof."""
    from apps.contacts import hcp

    _spend_proof(request)
    session = _session(request, payload.session_id)
    actor = hcp.contact_actor(request.contact)
    site = request.contact_token.app.name
    with transaction.atomic():
        person = _person(request, create=True)
        hcp.set_agent_memory(person, actor=actor,
                             record={"available": True, "default": True},
                             use={"available": payload.use, "default": payload.use})
        features = ["record"] + (["use"] if payload.use else [])
        try:
            hcp.issue_agent_grant(person, agent=session.agent, features=features,
                                  duration=hcp.TEMPORARY_FOR_SESSION, actor=actor, session=session,
                                  surface="embed-trusted",
                                  note=f"site={site}; identity=email verified by {site}")
        except hcp.HcpError as exc:
            raise _hcp_error(exc) from None
    return _state(request, session, person)


class ContactGrantIn(Schema):
    features: list[str]
    duration: str


@contact_hcp_router.post("/sessions/{session_id}/agent-grants", response=ContactHcpOut,
                         summary="Grant this conversation's agent, or keep it")
def contact_hcp_grant(request: HttpRequest, session_id: str, payload: ContactGrantIn) -> ContactHcpOut:
    """`duration=session`: let this conversation's agent learn / use, for this
    conversation. `duration=always`: keep what this conversation already allows,
    for that agent — a separate act (HCP 4.1.4). Needs a frame proof."""
    from apps.contacts import hcp

    _spend_proof(request)
    session = _session(request, session_id)
    person = _person(request, create=False)
    if person is None:
        raise HttpError(403, "opt in first")
    try:
        hcp.issue_agent_grant(person, agent=session.agent, features=payload.features,
                              duration=payload.duration, actor=hcp.contact_actor(request.contact),
                              session=session, surface="embed-trusted",
                              note=f"site={request.contact_token.app.name}")
    except hcp.HcpError as exc:
        raise _hcp_error(exc) from None
    return _state(request, session, person)


class SessionChoiceIn(Schema):
    record: str | None = None
    use: str | None = None


@contact_hcp_router.put("/sessions/{session_id}/memory", response=ContactHcpOut,
                        summary="Turn learning or use on or off for this conversation")
def contact_hcp_session_memory(request: HttpRequest, session_id: str,
                               payload: SessionChoiceIn) -> ContactHcpOut:
    """For this conversation only: `on`, `off` or `inherit` for `record` and `use`.
    Never turns on what you have not made available. Needs a frame proof."""
    from apps.contacts import hcp

    _spend_proof(request)
    session = _session(request, session_id)
    person = _person(request, create=False)
    if person is None:
        raise HttpError(403, "opt in first")
    try:
        hcp.set_session_memory(person, session, actor=hcp.contact_actor(request.contact),
                               record=payload.record, use=payload.use)
    except hcp.HcpError as exc:
        raise _hcp_error(exc) from None
    return _state(request, session, person)


class FeaturePolicyIn(Schema):
    available: bool | None = None
    default: bool | None = None


class PolicyIn(Schema):
    record: FeaturePolicyIn | None = None
    use: FeaturePolicyIn | None = None


@contact_hcp_router.put("/policy", response=ContactHcpOut,
                        summary="Change what agents may do with what they learn about me")
def contact_hcp_policy(request: HttpRequest, payload: PolicyIn) -> ContactHcpOut:
    """Your settings: for `record` and `use`, `available` and `default`. Turning
    both off is opting out — nothing is deleted. Needs a frame proof."""
    from apps.contacts import hcp

    _spend_proof(request)
    person = _person(request, create=False)
    if person is None:
        raise HttpError(403, "opt in first")
    hcp.set_agent_memory(person, actor=hcp.contact_actor(request.contact),
                         record=payload.record.model_dump() if payload.record else None,
                         use=payload.use.model_dump() if payload.use else None)
    return _state(request, None, person)


@contact_hcp_router.delete("/grants/{grant_id}", response=ContactHcpOut,
                           summary="Take back a grant")
def contact_hcp_revoke(request: HttpRequest, grant_id: str) -> ContactHcpOut:
    """Revoke one agent's access, immediately. Needs a frame proof."""
    from apps.contacts import hcp
    from apps.contacts.models import PersonGrant

    _spend_proof(request)
    person = _person(request, create=False)
    try:
        gid = uuid.UUID(grant_id.split(":")[-1])
    except ValueError:
        raise HttpError(404, "Not found") from None
    grant = PersonGrant.objects.filter(grant_id=gid, person=person).first() if person else None
    if grant is None:
        raise HttpError(404, "Not found")
    hcp.revoke_grant(grant, actor=hcp.contact_actor(request.contact))
    return _state(request, None, person)


@contact_hcp_router.delete("/entries/{entry_id}", response=ContactHcpOut,
                           summary="Remove something canopy holds about me")
def contact_hcp_delete_entry(request: HttpRequest, entry_id: str) -> ContactHcpOut:
    """Delete one entry (HCP 3.2.4: soft — it stays in your export and audit log as
    deleted, and no agent is told it again). Needs a frame proof."""
    from apps.contacts import hcp

    _spend_proof(request)
    person = _person(request, create=False)
    try:
        eid = hcp.parse_entry_id(entry_id)
    except hcp.HcpError:
        raise HttpError(404, "Not found") from None
    fact = hcp.current(eid) if person else None
    if fact is None or fact.person_id != person.pk:
        raise HttpError(404, "Not found")
    hcp.delete_entry(fact, actor=hcp.contact_actor(request.contact),
                     reason=f"removed by the person in {request.contact_token.app.name}'s panel")
    return _state(request, None, person)


@contact_hcp_router.get("/export", summary="Everything canopy holds about me")
def contact_hcp_export(request: HttpRequest) -> dict:
    """Every entry held about you (current versions, deleted ones included) and
    your audit log — the same as /api/hcp/v1/export with include=audit. Needs a
    frame proof."""
    from apps.contacts import hcp
    from apps.contacts.hcp_api import _event_dict
    from apps.contacts.models import PersonAuditEvent

    _spend_proof(request)
    person = _person(request, create=False)
    if person is None:
        return {"@context": hcp.CONTEXT, "entries": [], "audit": []}
    entries = hcp.current_versions(person).select_related(
        "person", "workspace", "project", "asserted_by_agent", "asserted_by_user")
    out = {"@context": hcp.CONTEXT, "subject": hcp.subject_id(person),
           "entries": [hcp.to_entry(f, redact=False) for f in entries.order_by("created_at")],
           "audit": [_event_dict(e) for e in
                     PersonAuditEvent.objects.filter(person=person).order_by("pk")]}
    hcp.audit(person, "preference.exported", actor=hcp.contact_actor(request.contact),
              detail=f"include=audit; from {request.contact_token.app.name}'s panel")
    return out
