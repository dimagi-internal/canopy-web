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

from apps.canopy_sessions.schemas import (
    MessagePageOut,
    PageActionOut,
    PageActionResultIn,
    PageActionsDeclareIn,
    PageActionSpec,
    PageStateIn,
    PageStateOut,
)
from apps.harness.schemas import TurnOut
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
    #: WHICH tenant this token is for, named by an agent the site may offer.
    #:
    #: A site can serve several canopy tenants, and a visitor's contact belongs
    #: to exactly one of them (the same human dealt with by two workspaces is
    #: deliberately two contacts), so the tenant has to be said rather than
    #: inferred. Omitted = the tenant that registered the site, which is what
    #: every integration written before this meant.
    agent_slug: str = ""


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

    try:
        workspace, grant = _tenant_for(app, payload.agent_slug)
    except HttpError:
        audit(event=EmbedAuditLog.EXCHANGE, request=request, app=app, ok=False,
              reason="not_granted", detail=f"agent={payload.agent_slug!r}")
        raise

    contact = contact_services.record_embed_visitor(
        workspace=workspace,
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

    user = contact_services.resolve_arrival(
        app=app, contact=contact, claims=claims,
        resolvable_domains=grant.resolvable_domains or [],
    )
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
    #: The routing SOURCE, as a user's send may declare it — so a host's work for a
    #: contact (ace-web's runs: `ace_web`) routes like the same host's work for a
    #: user. Only sources a caller may name; `email` and `slack` are channels
    #: canopy attests itself.
    origin: str = ""


def _tenant_for(app, agent_slug: str):
    """Which tenant this token is for, and that tenant's grant.

    Named by an AGENT, because that is the thing a host already knows when it
    mounts a widget and the only thing that identifies a tenant unambiguously —
    an agent belongs to exactly one workspace. The site must be granted by that
    tenant AND allowed to offer that agent: the first is the tenant saying "this
    site may act for us", the second "and it may offer this".

    Fails closed and says which of the two is missing, because "not authorized"
    with no reason sends an integrator to re-read their key configuration.
    """
    from apps.agents.models import Agent

    from .embed_apps import tenant_grant

    slug = (agent_slug or "").strip()
    if slug:
        agent = Agent.objects.filter(slug=slug).select_related("workspace").first()
        # Same answer for "no such agent" and "not offered here": a site must
        # not be able to discover another tenant's agents by guessing slugs.
        if agent is None or not app.allowed_agents.filter(agent=agent).exists():
            raise HttpError(403, f"not_granted: {app.name!r} does not offer an agent "
                                 f"named {slug!r}")
        workspace = agent.workspace
    else:
        if app.workspace_id is None:
            raise HttpError(
                409,
                f"{app.name!r} is not owned by a workspace, so there is no tenant to "
                "record its visitors in. Reconnect it from Connected sites.",
            )
        workspace = app.workspace
    grant = tenant_grant(app, workspace.slug if hasattr(workspace, "slug") else workspace)
    if grant is None:
        raise HttpError(403, f"not_granted: {workspace} has not authorized {app.name!r}")
    return workspace, grant


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
        from apps.harness.models import Turn

        if payload.origin and payload.origin not in (Turn.ORIGIN_API, Turn.ORIGIN_ACE_WEB):
            raise HttpError(422, f"origin {payload.origin!r} is not one a caller may name")
        message, turn = session_services.send_message(
            session=session, text=payload.text, user=request.user,
            client_id=payload.client_id, origin=payload.origin or None,
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


@contact_router.post("/sessions/{session_id}/attach", response=dict,
                     summary="I am watching this conversation (stream it live)")
def attach(request: HttpRequest, session_id: str) -> dict:
    """The same viewer signal a user's chat sends, so a contact watching their
    own conversation sees the agent's reply as it is written, not when it lands."""
    from apps.canopy_sessions import services as session_services

    return {"streaming": session_services.attach_session(_session_or_404(request, session_id))}


@contact_router.post("/sessions/{session_id}/detach", response=dict,
                     summary="I stopped watching")
def detach(request: HttpRequest, session_id: str) -> dict:
    from apps.canopy_sessions import services as session_services

    return {"streaming": session_services.detach_session(_session_or_404(request, session_id))}


# --- the page this contact is looking at --------------------------------------------
# The same two declarations a signed-in user's page makes, over the contact's OWN
# session only.
#
# These were missing rather than withheld, and the gap was load-bearing: the
# widget skipped both for a contact because "`/api/contact/` is their entire
# surface and neither declaration is on it", so an embedded agent could hold a
# conversation with a visitor and never learn what that visitor was looking at.
# A host could only get page state by having its visitors resolve to canopy
# ACCOUNTS — which needs a verified email on a domain the site may resolve, an
# existing canopy user, and a workspace membership. That made a page-aware panel
# a privilege of staff on their own products, for no reason a boundary asked for.
#
# Nothing here widens the boundary: the prefix is unchanged, the session is
# resolved by the same `_session_or_404`, and page state is strictly less
# sensitive than the message history a contact can already read. The agent's own
# read is unaffected — `page_visible_q` matches a contact's session through its
# AGENT leg, since such a session deliberately has no `created_by`.


@contact_router.put("/sessions/{session_id}/page-state", response=PageStateOut,
                    summary="What I am looking at")
def declare_page_state(request: HttpRequest, session_id: str,
                       payload: PageStateIn) -> PageStateOut:
    """Replaces the declaration wholesale, exactly as the user route does.

    A state over the server's cap is refused with `too_large`: send the
    selection (ids, filters) and the tool that resolves it, not the rows.
    """
    from apps.canopy_sessions import page_state

    session = _session_or_404(request, session_id)
    try:
        stored = page_state.set_page_state(session, payload.state)
    except page_state.PageStateError as exc:
        # 422 as on the user route: well-formed request, unacceptable CONTENT.
        raise HttpError(422, f"{exc.code}: {exc.message}")
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@contact_router.put("/sessions/{session_id}/page-actions", response=list[PageActionSpec],
                    summary="What my page can do")
def declare_page_actions(request: HttpRequest, session_id: str,
                         payload: PageActionsDeclareIn) -> list[PageActionSpec]:
    """Replaces the declaration wholesale — merging would leave the agent able
    to call into a page the visitor has left."""
    from apps.canopy_sessions import page_actions

    session = _session_or_404(request, session_id)
    page_actions.set_declared_actions(session, [a.dict() for a in payload.actions])
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@contact_router.post("/sessions/{session_id}/page-actions/{action_id}/result",
                     response=PageActionOut,
                     summary="My page reporting an action's outcome")
def resolve_page_action(request: HttpRequest, session_id: str, action_id: str,
                        payload: PageActionResultIn) -> PageActionOut:
    """Posted by the page after it runs the callback.

    Scoped to the contact's own session and then to that session's actions, so
    one page cannot resolve another's — the same two gates as the user route,
    with contact ownership standing where membership stands there.
    """
    import uuid as _uuid

    from apps.canopy_sessions import page_actions

    session = _session_or_404(request, session_id)
    try:
        pk = _uuid.UUID(str(action_id))
    except ValueError:
        raise HttpError(404, "no such page action on this session")
    action = session.page_actions.filter(pk=pk).first()
    if action is None:
        raise HttpError(404, "no such page action on this session")
    action = page_actions.resolve(action, result=payload.result, error=payload.error)
    return PageActionOut(id=str(action.id), name=action.name, status=action.status,
                         result=action.result, error=action.error)


# --- a host's work FOR a contact (ace-web's runs) ------------------------------------
# The same four things a host does for a USER while executing their command —
# stop, read a turn, read its transcript, ask whether any runner can take it —
# over the contact's OWN conversations only.

def _turn_or_404(request: HttpRequest, turn_id: str):
    import uuid as _uuid

    from apps.canopy_sessions.access import contact_session_q
    from apps.canopy_sessions.models import Session
    from apps.harness.models import Turn

    try:
        pk = _uuid.UUID(str(turn_id))
    except ValueError:
        raise HttpError(404, "turn not found") from None
    mine = Session.objects.filter(contact_session_q(request.contact)).values("pk")
    turn = (Turn.objects.select_related("chat_session", "initiator_user", "initiator_contact",
                                        "claimed_by")
            .filter(pk=pk, chat_session__in=mine).first())
    if turn is None:
        raise HttpError(404, "turn not found")
    return turn


@contact_router.post("/sessions/{session_id}/stop", response=dict,
                     summary="Cancel every unfinished turn in my conversation")
def stop(request: HttpRequest, session_id: str) -> dict:
    from apps.canopy_sessions import services as session_services

    return {"cancelled": session_services.cancel_session_turns(_session_or_404(request, session_id))}


@contact_router.get("/turns/unclaimable", response=list[dict],
                    summary="My queued turns no online runner can take")
def my_unclaimable(request: HttpRequest) -> list[dict]:
    from django.db.models import Q

    from apps.canopy_sessions.access import contact_session_q
    from apps.canopy_sessions.models import Session
    from apps.harness import services as harness_services

    mine = Session.objects.filter(contact_session_q(request.contact)).values("pk")
    return harness_services.unclaimable_queued_turns(
        ws_slugs={request.contact.workspace_id}, turn_q=Q(chat_session__in=mine))


@contact_router.get("/turns/{turn_id}", response=TurnOut, summary="One turn of my conversation")
def my_turn(request: HttpRequest, turn_id: str):
    return _turn_or_404(request, turn_id)


@contact_router.get("/turns/{turn_id}/transcript", summary="That turn's raw transcript")
def my_turn_transcript(request: HttpRequest, turn_id: str):
    from django.http import StreamingHttpResponse

    from apps.harness import services as harness_services

    return StreamingHttpResponse(harness_services.iter_transcript(_turn_or_404(request, turn_id)),
                                 content_type="application/x-ndjson")
