"""`/api/me/act-as-requests/` — an agent asking to act as the person who started its turn,
on canopy-web, and that person deciding with their passkey (`apps/harness/act_as.py`).

Two callers, kept apart on every route:

* **the agent**, with the login canopy-web issued for its turn: asks (POST), reads the
  status and collects the token once (GET `{id}`), stops it (POST `{id}/stop`). It reads
  only requests that same login made.
* **the person**, in their own browser: lists theirs (GET), approves with a WebAuthn
  assertion made for THIS request (POST `{id}/approve/begin` + `/approve/finish`), declines,
  stops. Approve and decline refuse every token: there must be a person at a browser.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from django.http import HttpRequest
from ninja import Field, Router, Schema

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.passkeys import services as passkeys
from apps.passkeys import stepup
from apps.passkeys.schemas import PasskeyAssertIn, PasskeyOptionsOut
from apps.tokens import kinds
from apps.workspaces import links

from . import act_as, acting_person
from .models import ActAsGrant

router = Router(auth=session_auth, tags=["act as you"])


class ActAsRequestIn(Schema):
    #: Why the agent needs to act as the person — shown to them verbatim.
    reason: str = Field(..., max_length=act_as.REASON_MAX)
    #: `read`, `write`, or `either` (the person picks; read-only is offered first).
    scope: str = "either"


class ActAsCreatedOut(Schema):
    id: uuid.UUID
    status: str
    #: Where the person approves, with their passkey. Pushed to their devices too.
    approve_url: str
    #: When the request lapses if nobody acts on it (10 minutes).
    expires_at: datetime


class ActAsRequestOut(Schema):
    id: uuid.UUID
    #: pending | approved | declined | expired | stopped
    status: str
    #: What the person granted (`read` | `write`), once approved; "" before.
    scope: str = ""
    requested_scope: str = ""
    #: Pending: when the request lapses. Approved: when the permission ends (30 minutes
    #: from the passkey touch, never extended).
    expires_at: datetime | None = None
    agent: str = ""
    agent_name: str = ""
    person: str = ""
    #: The agent's own words, verbatim.
    reason: str = ""
    turn_id: str = ""
    #: The conversation the turn runs in, when it has one.
    session_url: str = ""
    created_at: datetime | None = None
    approved_at: datetime | None = None


class ActAsStatusOut(ActAsRequestOut):
    #: The token, in the agent's FIRST read of a live approval only. Send it as
    #: `Authorization: Bearer <token>`; it signs in as the person. Never shown again.
    token: str | None = None


class ActAsApproveIn(PasskeyAssertIn):
    #: `read` or `write` — what the person grants.
    scope: str


def _problem(exc: act_as.Refused) -> ProblemError:
    type_ = {404: TYPE_NOT_FOUND, 409: TYPE_CONFLICT, 422: TYPE_VALIDATION}.get(exc.status, TYPE_FORBIDDEN)
    title = {404: "Not found", 409: "Conflict", 422: "Invalid request"}.get(exc.status, "Not allowed")
    return ProblemError(exc.status, title, type_=type_, detail=str(exc))


def _row(request_id) -> ActAsGrant | None:
    return (ActAsGrant.objects.select_related("person", "agent", "agent__workspace", "turn")
            .filter(pk=request_id).first())


def _is_asking_login(request: HttpRequest, grant: ActAsGrant) -> bool:
    """The same turn login that asked — the agent, never the person."""
    cred = getattr(request, "auth_credential", None) or {}
    return (kinds.kind_of(cred) is kinds.KINDS[kinds.PAT] and cred.get("id") is not None
            and int(cred["id"]) == grant.login_token_id and request.user.pk == grant.agent.user_id)


def _is_the_person(request: HttpRequest, grant: ActAsGrant) -> bool:
    """The person, acting as themselves — never a turn, a caller token or an act-as token."""
    return request.user.pk == grant.person_id and acting_person.is_person(request)


def _session_url(grant: ActAsGrant) -> str:
    sid = grant.turn.chat_session_id
    path = links.scoped_path_or_none(grant.agent.workspace, f"/sessions/{sid}") if sid else None
    return path or ""


def _out(grant: ActAsGrant) -> ActAsRequestOut:
    status = act_as.status_of(grant)
    expires = grant.expires_at if grant.approved_at else grant.request_expires_at
    return ActAsRequestOut(
        id=grant.pk, status=status, scope=grant.scope, requested_scope=grant.requested_scope,
        expires_at=expires, agent=grant.agent.slug,
        agent_name=grant.agent.name or grant.agent.slug, person=grant.person.email,
        reason=grant.reason, turn_id=str(grant.turn_id), session_url=_session_url(grant),
        created_at=grant.created_at, approved_at=grant.approved_at)


# --- the agent --------------------------------------------------------------------------------


@router.post("/", response={201: ActAsCreatedOut},
             summary="Ask to act as the person who started this turn, on canopy-web")
@gate("agent-login", kind="request")  # the turn's own login; the person decides
def create_act_as_request(request: HttpRequest, payload: ActAsRequestIn):
    """Call with the login canopy-web issued for your turn. canopy-web works out who
    started the turn (never from the request) and asks them — a push, their supervisor
    feed and Settings — to let you act as them for 30 minutes, read-only or read + write,
    with their passkey. 403 says why when there is no one to ask (a confined turn, one
    canopy or an agent started, an unverified asker, a stored token). Poll
    `GET /api/me/act-as-requests/{id}`; the request lapses after 10 minutes."""
    try:
        grant = act_as.ask(request.user, getattr(request, "auth_credential", None),
                           reason=payload.reason, scope=payload.scope)
    except act_as.Refused as exc:
        raise _problem(exc) from exc
    return 201, ActAsCreatedOut(id=grant.pk, status=grant.status,
                                approve_url=f"{links.base_url()}{act_as.approve_path(grant)}",
                                expires_at=grant.request_expires_at)


# Carries the token, which no browser ever steps up for: only the turn login that asked
# collects it, once, after the person's own passkey approved it.
@router.get("/{uuid:request_id}", response=ActAsStatusOut,
            summary="An act-as-you request's status (and, once, its token)")
@gate("agent-login", "self",
      step_up="the token goes only to the turn login that asked, once, after the person approved "
              "with a passkey; the person's own read never carries it")
def get_act_as_request(request: HttpRequest, request_id: uuid.UUID) -> ActAsStatusOut:
    """For the agent (the same turn login that asked): the status, and — in the first read
    after the person approves — the token, which signs in as them until `expires_at` or
    until the turn ends. For the person: the request, to decide on."""
    grant = _row(request_id)
    if grant is None:
        raise ProblemError(404, "Not found", type_=TYPE_NOT_FOUND)
    if _is_asking_login(request, grant):
        act_as.settle(grant)
        token = act_as.collect_token(grant) if grant.status == ActAsGrant.APPROVED else None
        return ActAsStatusOut(**_out(grant).model_dump(), token=token)
    if _is_the_person(request, grant):
        act_as.settle(grant)
        return ActAsStatusOut(**_out(grant).model_dump())
    raise ProblemError(404, "Not found", type_=TYPE_NOT_FOUND)


@router.post("/{uuid:request_id}/stop", response=ActAsRequestOut,
             summary="End an act-as-you request or permission now")
@gate("agent-login", "self", setting=True)
def stop_act_as_request(request: HttpRequest, request_id: uuid.UUID) -> ActAsRequestOut:
    """The agent (the turn login that asked) or the person ends it at once: a waiting
    request is withdrawn, a live token stops signing in from its next request."""
    grant = _row(request_id)
    if grant is None:
        raise ProblemError(404, "Not found", type_=TYPE_NOT_FOUND)
    if _is_asking_login(request, grant):
        by = "agent"
    elif _is_the_person(request, grant):
        by = "person"
    else:
        raise ProblemError(404, "Not found", type_=TYPE_NOT_FOUND)
    return _out(act_as.stop(grant, by=by))


# --- the person ----------------------------------------------------------------------------


@router.get("/", response=list[ActAsRequestOut],
            summary="Agents asking to act as me on canopy-web, and those that may")
@gate("self")
def list_my_act_as_requests(request: HttpRequest) -> list[ActAsRequestOut]:
    """Your requests still waiting for you, and the permissions still live: which agent,
    why, in which turn, what it may do, and until when."""
    rows = (ActAsGrant.objects.select_related("person", "agent", "agent__workspace", "turn")
            .filter(person=request.user, status__in=(ActAsGrant.PENDING, ActAsGrant.APPROVED))
            .order_by("-created_at")[:50])
    out = []
    for grant in rows:
        if act_as.settle(grant).status in (ActAsGrant.PENDING, ActAsGrant.APPROVED):
            out.append(_out(grant))
    return out


def _mine_in_browser(request: HttpRequest, request_id: uuid.UUID, what: str) -> ActAsGrant:
    stepup.require_browser(request, what)
    stepup.require_a_person(request, what)
    grant = _row(request_id)
    if grant is None or grant.person_id != request.user.pk:
        raise ProblemError(404, "Not found", type_=TYPE_NOT_FOUND)
    return grant


def _slot(grant: ActAsGrant) -> str:
    # One ceremony per request and person: a challenge begun for one cannot approve another.
    return f"act-as:{grant.pk}:{grant.person_id}"


@router.post("/{uuid:request_id}/approve/begin", response=PasskeyOptionsOut,
             summary="Start approving an agent acting as me, with my passkey")
@gate("self", kind="ephemeral-credential")  # a ceremony's challenge, for the person's own request
def begin_approve_act_as_request(request: HttpRequest, request_id: uuid.UUID) -> PasskeyOptionsOut:
    """Options for navigator.credentials.get(), to approve THIS request with one of your
    passkeys. A browser session only; no token can."""
    grant = _mine_in_browser(request, request_id, "approving an act-as-you permission")
    if act_as.status_of(grant) != ActAsGrant.PENDING:
        raise ProblemError(409, "Conflict", type_=TYPE_CONFLICT,
                           detail=f"this request is {act_as.status_of(grant)}")
    options = passkeys.begin_assertion(request, slot=_slot(grant))
    if options is None:
        raise ProblemError(409, "No passkey registered", type_=TYPE_CONFLICT,
                           detail="Register a passkey (Settings → Passkeys), then approve.",
                           members={"error": "no_passkey", "register_url": stepup.REGISTER_PATH})
    return PasskeyOptionsOut(options=options)


@router.post("/{uuid:request_id}/approve/finish", response=ActAsRequestOut,
             summary="Approve an agent acting as me, with my passkey")
@gate("self", setting=True)
def finish_approve_act_as_request(request: HttpRequest, request_id: uuid.UUID,
                                  payload: ActAsApproveIn) -> ActAsRequestOut:
    """Verify the passkey assertion begun for this request, then let the agent act as you
    — `read` (it may read, never change) or `write` — for 30 minutes from now, while its
    turn runs. A browser session only; no token can."""
    grant = _mine_in_browser(request, request_id, "approving an act-as-you permission")
    try:
        passkeys.finish_assertion(request, payload.credential, slot=_slot(grant))
    except passkeys.CeremonyError as exc:
        raise ProblemError(400, "Passkey verification failed", type_=TYPE_VALIDATION,
                           detail=str(exc)) from exc
    try:
        grant = act_as.approve(grant, request.user, payload.scope)
    except act_as.Refused as exc:
        raise _problem(exc) from exc
    return _out(grant)


@router.post("/{uuid:request_id}/decline", response=ActAsRequestOut,
             summary="Decline an agent acting as me")
@gate("self", setting=True)
def decline_act_as_request(request: HttpRequest, request_id: uuid.UUID) -> ActAsRequestOut:
    """Say no. A browser session only; no token can."""
    grant = _mine_in_browser(request, request_id, "declining an act-as-you request")
    try:
        return _out(act_as.decline(grant, request.user))
    except act_as.Refused as exc:
        raise _problem(exc) from exc
