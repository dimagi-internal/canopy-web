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
from ninja.errors import HttpError
from ninja.security import HttpBearer

from . import assertions
from .audit import record as audit
from .models import ContactToken, EmbedAuditLog

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
