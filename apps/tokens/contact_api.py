"""`/api/contact/…` — everything a person with no canopy account may reach.

**The prefix is the security boundary, not a convention.** A `ContactToken`
produces no `request.user`, so `LoginRequiredMiddleware` refuses every path in
canopy except the ones listed there — and `/api/contact/` is the only prefix
listed. A surface becomes reachable by a contact by being moved here
deliberately, never by a view forgetting to check who is calling.

That is the opposite of the arrangement that failed before. `Agent.workspace`
was nullable and six predicates each grew a "no tenant ⇒ allow" leg, because
the decision was distributed across everything that happened to receive one.
Here it is made once, in a tuple of path prefixes.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest
from ninja import Router, Schema

from apps.canopy_sessions.schemas import MessagePageOut
from ninja.errors import HttpError
from ninja.security import HttpBearer

from apps.harness import initiator as who

from . import assertions
from .audit import client_ip, record as audit
from .models import ContactToken, EmbedAuditLog
from .rate_limit import (
    ContactTokenRateLimitError,
    check_contact_token_client,
    check_contact_token_issuer,
)

log = logging.getLogger(__name__)

#: A contact's session is short and re-established from the host's assertion,
#: which the host can mint again at any time without troubling the visitor.
#: There is no refresh: a contact's session is a visit, not a login.
CONTACT_TOKEN_TTL_SECONDS = 30 * 60


class ContactAuth(HttpBearer):
    """Requires a resolved contact — never a user, never an app on its own."""

    def authenticate(self, request: HttpRequest, token: str):
        contact = getattr(request, "contact", None)
        if contact is None:
            return None
        # Returned as the "user" Ninja stamps on the request; the views read
        # `request.contact`. Returning the contact rather than True keeps
        # `request.auth` meaningful in a traceback.
        return contact


contact_auth = ContactAuth()
contact_router = Router(auth=contact_auth, tags=["contact"])

#: Unauthenticated: the assertion IS the credential.
contact_token_router = Router(auth=None, tags=["contact"])


class ContactTokenIn(Schema):
    assertion: str


class ContactTokenOut(Schema):
    token: str
    expires_at: str
    contact_id: int
    display_name: str
    #: "user" when the visitor has an existing canopy account they arrive as
    #: (the token is then a delegated USER token — their own ACL); "contact"
    #: otherwise. The widget does not need this (it discovers its principal),
    #: but a host that behaves differently for its canopy users can read it.
    kind: str = "contact"


class ContactAgentOut(Schema):
    slug: str
    name: str
    description: str


class ContactMeOut(Schema):
    contact_id: int
    display_name: str
    identity: str
    app: str
    agents: list[ContactAgentOut]


_STATUS = {
    "malformed": 400, "no_issuer": 400, "incomplete": 400, "no_subject": 400,
    "no_jti": 400, "unknown_issuer": 401, "bad_signature": 401, "no_key": 401,
    "wrong_audience": 401, "expired": 401, "too_long": 400, "replayed": 401,
    "rate_limited": 429,
}


@contact_token_router.post("/contact-token", response=ContactTokenOut, auth=None,
                           summary="Exchange a signed assertion for a contact token")
def contact_token(request: HttpRequest, payload: ContactTokenIn) -> ContactTokenOut:
    """A connected site vouches for one of its visitors, and gets them a session.

    No `Authorization` header: the assertion is the credential, and that is the
    point. A shared secret would still have to exist, be distributed, and be
    the thing an attacker looks for — whereas a signature proves the claim
    without canopy holding anything that could make one.

    The workspace comes from the app's own row, never the assertion. A host
    cannot name the tenant it wants its visitor placed in.
    """
    from apps.contacts import services as contact_services

    # Two budgets, in the order the work happens. This endpoint is
    # unauthenticated: the assertion IS the credential, so there is nobody to
    # bill until one has been checked, and a limit checked after the signature
    # has not saved the CPU the signature cost.
    try:
        check_contact_token_client(client_ip(request))
    except ContactTokenRateLimitError as exc:
        audit(event=EmbedAuditLog.EXCHANGE, request=request, ok=False,
              reason="rate_limited", detail="per-client, before parsing")
        raise HttpError(429, f"rate_limited: {exc}")

    try:
        # Named, not yet trusted. Nothing may act on this app until the
        # signature verifies below — it is read here only to bill the right
        # budget for the verification it is about to ask for.
        claimed = assertions.issuer_of(payload.assertion)
    except assertions.AssertionError_ as exc:
        audit(event=EmbedAuditLog.EXCHANGE, request=request, ok=False,
              reason=exc.code, detail="signed assertion")
        raise HttpError(_STATUS.get(exc.code, 401), f"{exc.code}: {exc.message}")

    try:
        check_contact_token_issuer(claimed.name)
    except ContactTokenRateLimitError as exc:
        audit(event=EmbedAuditLog.EXCHANGE, request=request, app=claimed, ok=False,
              reason="rate_limited", detail="per-issuer")
        raise HttpError(429, f"rate_limited: {exc}")

    try:
        app, claims = assertions.verify_for_issuer(payload.assertion)
    except assertions.AssertionError_ as exc:
        audit(event=EmbedAuditLog.EXCHANGE, request=request, ok=False,
              reason=exc.code, detail="signed assertion")
        raise HttpError(_STATUS.get(exc.code, 401), f"{exc.code}: {exc.message}")

    if app.workspace_id is None:
        raise HttpError(
            409,
            f"{app.name!r} is not owned by a workspace, so there is no tenant to "
            "record its visitors in. Reconnect it from Connected sites.",
        )

    contact = contact_services.record_embed_visitor(
        workspace=app.workspace,
        app=app,
        external_id=str(claims.get("sub") or ""),
        email=str(claims.get("email") or ""),
        display_name=str(claims.get("name") or ""),
        # A verified signature over this specific visitor. Tier 2 on the shared
        # ladder — the same standing as a DKIM-signed message, and honestly not
        # more: it proves the SITE said this, not that the human is who the site
        # thinks. Person-level identity still needs `promote_to_user`.
        grade="app_signed",
        detail=f"jti={claims.get('jti')} iss={claims.get('iss')}",
    )
    if contact is None:
        raise HttpError(400, "the assertion does not identify a visitor")

    user = contact_services.resolve_arrival(app=app, contact=contact, claims=claims)
    if user is not None:
        # An existing canopy account arrives AS ITSELF: a delegated user token,
        # the same short-lived revocable row canopy's own widget mints. Never a
        # new account — resolve_arrival only finds, it does not create.
        from .models import DelegatedToken

        raw, token = DelegatedToken.issue(app=app, user=user, ttl_seconds=CONTACT_TOKEN_TTL_SECONDS,
                                          assurance=DelegatedToken.ASSURANCE_HOST_SIGNED)
        audit(event=EmbedAuditLog.EXCHANGE, request=request, app=app, subject=user,
              detail=f"contact={contact.identity} arrived as user {user.pk} "
                     f"(assurance=host_signed) ttl={CONTACT_TOKEN_TTL_SECONDS}s")
        return ContactTokenOut(token=raw, expires_at=token.expires_at.isoformat(),
                               contact_id=contact.pk, display_name=contact.display_name,
                               kind="user")

    raw, token = ContactToken.issue(
        app=app, contact=contact, ttl_seconds=CONTACT_TOKEN_TTL_SECONDS
    )
    audit(event=EmbedAuditLog.EXCHANGE, request=request, app=app,
          detail=f"contact={contact.identity} grade=app_signed ttl={CONTACT_TOKEN_TTL_SECONDS}s")
    return ContactTokenOut(
        token=raw,
        expires_at=token.expires_at.isoformat(),
        contact_id=contact.pk,
        display_name=contact.display_name,
    )


@contact_router.get("/me", response=ContactMeOut, summary="Who canopy thinks I am")
def contact_me(request: HttpRequest) -> ContactMeOut:
    """The contact's own view: themselves, and the agents this site may offer.

    Deliberately not the workspace's agent index. A contact is not a member, so
    the list is the app's allowlist and nothing is intersected with memberships
    they do not have — the same three-way question `/api/embed/agents` answers
    for users, minus the leg that does not apply.
    """
    from apps.agents.models import Agent

    contact = request.contact
    app = request.delegated_app
    rows = (
        Agent.objects.filter(embedding_apps__app=app, workspace_id=contact.workspace_id)
        .order_by("slug")
        .distinct()
    )
    return ContactMeOut(
        contact_id=contact.pk,
        display_name=contact.display_name,
        identity=contact.identity,
        app=app.name,
        agents=[
            ContactAgentOut(slug=a.slug, name=a.name, description=a.description)
            for a in rows
        ],
    )


# --- a contact's own conversations --------------------------------------------
#
# Mirrored here rather than opened up on `/api/canopy-sessions/`, and the
# duplication is the point: those routes stay strictly user-only, so a contact
# cannot reach one by a view forgetting to ask who is calling. Each of these is
# a thin delegation to the same service the user-facing route calls, so the two
# cannot drift on behaviour — only on who is allowed through.


class ContactSessionOut(Schema):
    id: str
    agent_slug: str | None
    title: str
    status: str
    created_at: str
    #: The host's own descriptive keys, as it set them (e.g. ace-web's
    #: `origin_key`, `opp_slug`) — never canopy's (`host_metadata`).
    metadata: dict = {}


class ContactSessionCreateIn(Schema):
    agent_slug: str
    title: str = ""
    #: The host's link for this conversation — the same rule as a user's
    #: (`canopy_sessions.services.host_metadata`), so a contact's chat carries
    #: e.g. its opportunity exactly as a user's does.
    metadata: dict = {}


class ContactSendIn(Schema):
    text: str
    client_id: str = ""


def _session_or_404(request: HttpRequest, session_id):
    """This contact's session, or 404. Never anybody else's.

    `contact_session_q` is disjoint from `visible_session_q` by construction,
    so there is no ordering of gates here to get wrong — a user's conversation
    simply is not in the set this can return.
    """
    from apps.canopy_sessions.access import contact_session_q
    from apps.canopy_sessions.models import Session

    session = (
        Session.objects.select_related("agent")
        .filter(contact_session_q(request.contact))
        .filter(pk=session_id)
        .first()
    )
    if session is None:
        raise HttpError(404, "session not found")
    return session


def _session_out(s) -> ContactSessionOut:
    from apps.canopy_sessions.services import SERVER_OWNED_METADATA

    return ContactSessionOut(
        id=str(s.id),
        agent_slug=s.agent.slug if s.agent_id else None,
        title=s.title,
        status=s.status,
        created_at=s.created_at.isoformat(),
        metadata={k: v for k, v in (s.metadata or {}).items() if k not in SERVER_OWNED_METADATA},
    )


@contact_router.post("/sessions", response=ContactSessionOut,
                     summary="Start a conversation with an agent this site offers")
def start_session(request: HttpRequest, payload: ContactSessionCreateIn) -> ContactSessionOut:
    """The agent must be one the SITE was allowed to offer.

    Not one the contact can reach — a contact reaches nothing, having no
    membership. So this is the app's allowlist intersected with the contact's
    own workspace, and there is deliberately no third leg.
    """
    from apps.agents.models import Agent

    contact = request.contact
    app = request.delegated_app
    agent = (
        Agent.objects.filter(
            slug=payload.agent_slug,
            embedding_apps__app=app,
            workspace_id=contact.workspace_id,
        )
        .first()
    )
    if agent is None:
        raise HttpError(404, f"{payload.agent_slug!r} is not offered here")

    from apps.canopy_sessions import services as session_services

    try:
        metadata = session_services.host_metadata(payload.metadata)
    except ValueError as exc:
        raise HttpError(422, str(exc))
    metadata["embed_app"] = app.name          # server-owned: which site, from the token
    # The same constructor a user's session goes through — so a contact's
    # conversation is recorded the same way (its transcript is its record under a
    # real runner) — with no `created_by`: there is no user, and leaving it null is
    # what keeps this row out of `visible_session_q` for every member of the tenant.
    session = session_services.create_session(
        workspace=contact.workspace, agent=agent, contact=contact,
        title=(payload.title or "")[:200], metadata=metadata,
    )
    audit(event=EmbedAuditLog.MINT, request=request, app=app,
          detail=f"contact={contact.identity} started session {session.id} with {agent.slug}")
    return _session_out(session)


@contact_router.get("/sessions", response=list[ContactSessionOut],
                    summary="My conversations on this site")
def list_sessions(request: HttpRequest, source: str = "", origin_key: str = "",
                  opp_slug: str = "", opp_run_id: str = "", resource: str = "",
                  page_path: str = "") -> list[ContactSessionOut]:
    """The same host filters a user's list takes, over the contact's OWN
    conversations only — a filter narrows, it never widens what `contact_session_q`
    already allows."""
    from apps.canopy_sessions.access import contact_session_q
    from apps.canopy_sessions.models import Session

    rows = Session.objects.select_related("agent").filter(contact_session_q(request.contact))
    for field, value in (("metadata__source", source), ("metadata__origin_key", origin_key),
                         ("metadata__opp_slug", opp_slug), ("metadata__opp_run_id", opp_run_id),
                         ("page_state__resource", resource), ("page_state__path", page_path)):
        if value:
            rows = rows.filter(**{field: value})
    return [_session_out(s) for s in rows.order_by("-created_at")[:50]]


@contact_router.get("/sessions/{session_id}", response=ContactSessionOut,
                    summary="One of my conversations")
def get_session(request: HttpRequest, session_id: str) -> ContactSessionOut:
    return _session_out(_session_or_404(request, session_id))


@contact_router.post("/sessions/{session_id}/send", response=dict,
                     summary="Say something")
def send(request: HttpRequest, session_id: str, payload: ContactSendIn) -> dict:
    from apps.canopy_sessions import services as session_services

    session = _session_or_404(request, session_id)
    if not payload.text.strip():
        raise HttpError(422, "message text is required")
    try:
        # `user` is the anonymous request user. `enqueue_turn` ignores an
        # unauthenticated one, so the turn simply carries no `enqueued_by` —
        # which is correct: nobody with an account sent this. A routing rule
        # that wants to know reads `turn.session.contact`, which is the
        # authoritative answer rather than a copy.
        message, turn = session_services.send_message(
            session=session, text=payload.text, user=request.user,
            client_id=payload.client_id,
            initiator=who.for_request(request, via=who.channel(request, "contact")),
        )
    except ValueError as exc:
        raise HttpError(422, str(exc))
    session_services.maybe_execute_inline(turn)
    return {"turn_id": str(turn.id) if turn else None, "message_id": message.id}


@contact_router.get("/sessions/{session_id}/messages", response=MessagePageOut,
                    summary="Earlier messages")
def messages(request: HttpRequest, session_id: str, before: int, limit: int = 50):
    """The SAME page a user's scroll-back returns (`MessagePageOut`), so a chat UI
    renders a contact's conversation with the one renderer it already has.

    (It used to hand-build rows from `m.body`, a field `Message` does not have —
    every call on a conversation with a message in it was a 500.)
    """
    from apps.api.pagination import clamp_limit
    from apps.canopy_sessions import services as session_services
    from apps.canopy_sessions.schemas import MessageOut

    session = _session_or_404(request, session_id)
    rows, has_more = session_services.messages_before(
        session, before=before, limit=clamp_limit(limit)
    )
    return {"messages": [MessageOut.from_orm(m) for m in rows], "has_more_before": has_more}
