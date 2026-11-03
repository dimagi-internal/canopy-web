"""Who is in a session: participants, their copies of a private chat's key, join requests."""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpRequest
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.agents.credentials import login_test_key
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.route_gates import gate
from apps.harness import sensitivity
from apps.workspaces import services as wsvc

from . import (
    access,
    conversation_keys,
    serializers,
    services,
)
from . import join_requests
from .models import Session, SessionParticipant
from .schemas import (
    KeyCopyIn,
    MyKeyCopiesIn,
    MyKeyCopiesOut,
    MyKeyCopyOut,
    MyVaultKeyOut,
    ParticipantAddIn,
    ParticipantCandidateOut,
    ParticipantRecipientOut,
    ParticipantOut,
    JoinRequestOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _in_it_or_403,
    _session_or_404,
    router,
)

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


@router.get("/my-vault-key", response=MyVaultKeyOut, summary="My vault public key, to wrap a new chat's key to")
@gate("self")
def my_vault_key(request: HttpRequest):
    """Your vault key's PUBLIC half and its thumbprint, for your browser to wrap a new
    conversation's key to before it starts one (`SessionCreateIn.conversation_key`) —
    also from the embedded panel, which has no browser session to read
    `/api/vault/keyset` with. With it, to your own browser only, the keyset that opens
    it: a browser that has not opened your vault key before derives its public half
    from it before trusting this one. 404 when you have none."""
    found = conversation_keys.recipient(request.user)
    if found is None:
        raise HttpError(404, "you have no vault key")
    # The seals go only to the person's browser, as `sealed_key.keyset` does.
    keyset = (conversation_keys.keyset_for_browser(request.user)
              if sensitivity.request_is_chat_surface(request)
              and sensitivity.request_is_panel_browser(request) else None)
    return {"user_id": str(found["user_id"]), "public_jwk": found["public_jwk"], "jkt": found["jkt"],
            "keyset": keyset}


def _own_browser_or_403(request: HttpRequest) -> None:
    # Your conversation keys are re-wrapped where your vault key is set up: canopy's own
    # signed-in page. Not MCP, a token, a contact, or a site's delegated token.
    if (getattr(request, "delegated_app", None) or getattr(request, "contact", None)
            or not sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, "your conversation keys are re-wrapped from canopy in your browser")


@router.get("/my-keys", response=list[MyKeyCopyOut],
            summary="The private chats whose key I hold a copy of")
@gate("self")
def my_key_copies(request: HttpRequest):
    """Before you replace your vault key: every sensitive conversation you hold a copy
    of the key to (a wrap only your vault key opens — never what was said), so your
    browser can re-wrap each to the new key while the old one is open, and name them
    to you. Store the re-wrapped copies with `PUT /my-keys` once the new key is set.
    Canopy's own signed-in page only."""
    _own_browser_or_403(request)
    return conversation_keys.my_copies(request.user)


@router.put("/my-keys", response=MyKeyCopiesOut,
            summary="Store my private chats' keys, re-wrapped to my new vault key")
@gate("self", setting=True)
def put_my_key_copies(request: HttpRequest, payload: MyKeyCopiesIn):
    """After `PUT /api/vault/keyset` with `replace`: your copies of your conversations'
    keys, each re-wrapped in your browser to your NEW vault key. Replaces only copies
    you already hold, and only to your current vault key; 422 (storing none) when one
    is not."""
    _own_browser_or_403(request)
    try:
        n = conversation_keys.replace_my_copies(request.user, [
            {"session_id": c.session_id, "wrap": c.wrap.model_dump(), "recipient_jkt": c.recipient_jkt}
            for c in payload.copies])
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None
    return {"stored": n}


# ---- join requests: asking to be added to a private conversation ----

def _join_request_out(req) -> dict:
    return join_requests.dto(req)


def _join_request_or_404(request: HttpRequest, request_id: uuid.UUID):
    req = (join_requests.visible_to(request.user).select_related("session", "requested_by", "decided_by")
           .filter(pk=request_id).first())
    if req is None:
        raise HttpError(404, "join request not found")
    return req


@router.post("/join-requests/{request_id}/decline", response=JoinRequestOut,
             summary="Decline someone's request to join your private conversation")
@gate("session-acl", kind="conversation")  # its owner, in it (`join_requests.may_decide`)
def decline_join_request(request: HttpRequest, request_id: uuid.UUID):
    """They stay out. To let them in, add them (`POST /{id}/participants`, from your
    browser, which gives them their copy of the conversation's key): that answers
    the request."""
    req = _join_request_or_404(request, request_id)
    try:
        join_requests.decline(req, request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


@router.post("/join-requests/{request_id}/cancel", response=JoinRequestOut,
             summary="Withdraw your request to join a private conversation")
@gate("self", kind="conversation")  # only whoever asked
def cancel_join_request(request: HttpRequest, request_id: uuid.UUID):
    req = _join_request_or_404(request, request_id)
    try:
        join_requests.cancel(req, request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


def _participants(session: Session) -> list[dict]:
    rows = list(session.participants.select_related("user").order_by("created_at"))
    # Who in a sensitive conversation cannot read it yet, for its people to give a copy.
    keyless = (conversation_keys.without_current_copy(session, [p.user_id for p in rows])
               if sensitivity.is_sensitive(session) else set())
    test_keys = login_test_key.logins_with_test_key([p.user_id for p in rows])
    return [{**serializers.participant_dto(p), "needs_key": p.user_id in keyless,
             "test_key": p.user_id in test_keys} for p in rows]


@router.get("/{session_id}/participants", response=list[ParticipantOut],
            summary="Who has been given this chat")
@gate("session-acl")
def list_participants(request: HttpRequest, session_id: uuid.UUID):
    """Everyone explicitly in this conversation, with their role."""
    return _participants(_session_or_404(request, session_id))


@router.get("/{session_id}/participants/candidates", response=list[ParticipantCandidateOut],
            summary="Who you could add to this chat")
@gate("session-acl")  # can_add_people
def participant_candidates(request: HttpRequest, session_id: uuid.UUID):
    """The workspace's members you may add: not in it yet, not its creator. Agents'
    own logins carry their agent's slug. 403 unless you
    may add people (`access.can_add_people`)."""
    session = _session_or_404(request, session_id)
    if not access.can_add_people(request.user, session):
        raise HttpError(403, "only the chat's owner can share it")
    taken = set(session.participants.values_list("user_id", flat=True))
    taken.update(i for i in (session.created_by_id, request.user.pk) if i)
    users = [m.user for m in wsvc.effective_memberships(session.workspace_id) if m.user_id not in taken]
    ids = [u.pk for u in users]
    agents = dict(Agent.objects.filter(user_id__in=ids).values_list("user_id", "slug"))
    rows = [{"user_id": u.pk, "email": u.email,
             "display_name": (u.get_full_name() or "").strip() or u.email,
             "agent_slug": agents.get(u.pk, "")}
            for u in users if u.email and u.is_active]
    return sorted(rows, key=lambda r: (bool(r["agent_slug"]), r["display_name"].lower()))


@router.post("/{session_id}/participants", response=list[ParticipantOut],
             summary="Give a teammate this chat")
@gate("session-acl", setting=True)  # can_add_people (chat owner, or the agent's owner in it)
def add_participant(request: HttpRequest, session_id: uuid.UUID, payload: ParticipantAddIn):
    """The chat's owner, or the owner of the agent it is with when they are in it.
    The teammate must already be a member of the chat's workspace; the chat's owner
    adding someone again changes their role."""
    session = _session_or_404(request, session_id)
    if not access.can_add_people(request.user, session):
        raise HttpError(403, "only the chat's owner can share it")
    _in_it_or_403(request, session)
    target = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if target is None or not wsvc.is_member(target, session.workspace_id):
        raise HttpError(404, "nobody with that email is in this workspace")
    if target.pk == session.created_by_id:
        raise HttpError(409, "that person owns this chat")
    # The agent's owner brings someone NEW in; changing a role stays the chat owner's.
    if (not access.can_share(request.user, session)
            and SessionParticipant.objects.filter(session=session, user=target).exists()):
        raise HttpError(409, "they are already in this chat; only its owner can change their role")
    with transaction.atomic():
        # In it first, then their copy of the key: a copy goes only to someone in the
        # conversation (an agent login's test key is held to that, `add_wrap`). A
        # refused wrap rolls the add back.
        SessionParticipant.objects.update_or_create(
            session=session, user=target, defaults={"role": payload.role})
        if sensitivity.is_sensitive(session):
            _store_wrap(request, session, target, payload.wrap)
        # Their request to be added, if they asked, is answered by this.
        join_requests.settle_on_add(session, target, by=request.user)
    return _participants(session)


def _store_wrap(request: HttpRequest, session, target, wrap) -> None:
    """A sensitive conversation's new person needs their copy of its key, which only a
    browser already in it can make (canopy-web cannot unwrap the key). Not needed again
    for a role change: they already hold a current one."""
    # Someone who already holds a current copy keeps it (a role change, a re-add): an
    # overwrite could lock them out (`conversation_keys.CopyExists`).
    if conversation_keys.has_current_wrap(session, target):
        return
    if wrap is None:
        raise ProblemError(409, "Their copy of the key is needed", type_=TYPE_CONFLICT,
                           detail="this conversation is sealed to its people: add them from a "
                                  "browser already in it, which wraps its key to their vault key",
                           members={"error": "wrap_required"})
    try:
        conversation_keys.add_wrap(session, target, wrap.model_dump(exclude={"recipient_jkt"}),
                                   recipient_jkt=wrap.recipient_jkt, by=request.user)
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None


@router.get("/{session_id}/participants/recipient", response=ParticipantRecipientOut,
            summary="The vault key to wrap a sensitive chat's key to")
@gate("session-acl")  # can_share (chat owner), and in the conversation
def participant_recipient(request: HttpRequest, session_id: uuid.UUID, email: str):
    """Before adding someone to a SENSITIVE conversation: their user id and current
    vault public key, which your browser wraps the conversation's key to. 404 when
    nobody with that email is in the workspace; 409 `no_vault_key` when they have no
    vault key yet (they could not read it)."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    target = get_user_model().objects.filter(email__iexact=email.strip()).first()
    if target is None or not wsvc.is_member(target, session.workspace_id):
        raise HttpError(404, "nobody with that email is in this workspace")
    # Its owner asks before adding someone; anyone in it, before giving a copy of the key
    # to someone already in it (`POST /{id}/key-copies`).
    if not access.can_share(request.user, session) and not sensitivity.is_conversation_person(
            session, user=target):
        raise HttpError(403, "only the chat's owner can share it")
    found = conversation_keys.recipient(target)
    if found is None:
        raise ProblemError(409, "No vault key", type_=TYPE_CONFLICT,
                           detail=f"{target.email} has no vault key yet, so they cannot read a "
                                  "sensitive conversation; ask them to set one up first",
                           members={"error": "no_vault_key"})
    return {**found, "email": target.email, "test_key": not login_test_key.is_person(target)}


@router.post("/{session_id}/key-copies", response=list[ParticipantOut],
             summary="Give someone in a private chat their copy of its key")
@gate("session-acl", setting=True)  # in the conversation, holding a current copy
def give_key_copy(request: HttpRequest, session_id: uuid.UUID, payload: KeyCopyIn):
    """For someone ALREADY in a sensitive conversation who cannot read it: given it
    before it became sensitive, or whose vault key changed since (its starter
    included). Your browser opens your copy of the key and wraps it to their current
    vault key (`GET /{id}/participants/recipient`); canopy-web stores the wrap. An editor
    or the owner who holds a current copy may give one (403 otherwise), and only to
    someone holding no copy for their current vault key: 409 when they do. A copy for
    an older vault key is replaced; one that does not open is set aside by its holder
    first (`DELETE /{id}/key-copies/mine`). It puts nobody in it: 404 when they are not
    in it."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    if not access.can_write(request.user, session):
        raise HttpError(403, "only an editor or the owner of this conversation can give "
                             "someone a copy of its key")
    if not sensitivity.is_sensitive(session) or conversation_keys.key_of(session) is None:
        raise HttpError(409, "this conversation has no key to give")
    if not conversation_keys.has_current_wrap(session, request.user):
        raise HttpError(409, "your own copy of its key is not for your current vault key; "
                             "ask someone else in it to give you a fresh one first")
    target = get_user_model().objects.filter(pk=payload.user_id).first()
    if target is None or not sensitivity.is_conversation_person(session, user=target):
        raise HttpError(404, "nobody with that id is in this conversation")
    try:
        conversation_keys.add_wrap(session, target, payload.wrap.model_dump(exclude={"recipient_jkt"}),
                                   recipient_jkt=payload.wrap.recipient_jkt, by=request.user)
    except conversation_keys.CopyExists as exc:
        raise HttpError(409, str(exc)) from None
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None
    return _participants(session)


@router.delete("/{session_id}/key-copies/mine", response=list[ParticipantOut],
               summary="Set aside your copy of a private chat's key that does not open")
@gate("session-acl", setting=True)  # your own copy, in the conversation
def set_aside_my_key_copy(request: HttpRequest, session_id: uuid.UUID):
    """Your copy of this conversation's key does not open in your browser (it was
    damaged, or wrapped to a vault key you no longer have). Setting it aside drops it,
    so you are listed `needs_key` and someone in it can give you a fresh one
    (`POST /{id}/key-copies`). Only you can do this for your copy."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    conversation_keys.set_aside_my_copy(session, request.user)
    return _participants(session)


@router.delete("/{session_id}/participants/{user_id}", response=list[ParticipantOut],
               summary="Take a chat away from someone")
@gate("session-acl", setting=True)  # can_share, or self-removal
def remove_participant(request: HttpRequest, session_id: uuid.UUID, user_id: int):
    """The owner can remove anyone but themselves; anyone can remove themselves."""
    session = _session_or_404(request, session_id)
    if user_id == session.created_by_id:
        raise HttpError(409, "the chat's owner cannot be removed")
    if user_id != request.user.pk and not access.can_share(request.user, session):
        raise HttpError(403, "only the chat's owner can remove someone else")
    SessionParticipant.objects.filter(session=session, user_id=user_id).delete()
    # Their copy of a sensitive conversation's key goes with them. (The key itself is
    # not rotated: they could have kept what they read.)
    conversation_keys.drop_wrap(session, user_id)
    # A tab they still hold on it re-asks who may read it — for a sensitive
    # conversation, someone no longer in it must stop hearing it now.
    transaction.on_commit(lambda: services.recheck_sockets(session.pk, user_ids=[user_id]))
    return _participants(session)


@router.post("/{session_id}/join-requests", response=JoinRequestOut,
             summary="Ask to be added to a private conversation")
@gate("session-acl", kind="conversation")  # you can see it exists, and are not in it
def ask_to_join(request: HttpRequest, session_id: uuid.UUID):
    """A private (sensitive) conversation you can see listed but are not in: ask its
    owner to add you. The request names you and the conversation, nothing it said.
    Asking again returns the request already open. 409 for an ordinary
    conversation, or one you are already in; 429 when your last request into it,
    declined or withdrawn, was made within the hour."""
    session = _session_or_404(request, session_id)
    try:
        req = join_requests.ask(session, request.user)
    except join_requests.TooSoon as exc:
        raise HttpError(429, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


@router.get("/{session_id}/join-requests", response=list[JoinRequestOut],
            summary="Open requests to join a private conversation you are in")
@gate("session-acl")  # its people; anyone else reads none
def list_join_requests(request: HttpRequest, session_id: uuid.UUID):
    """The people asking to be added, oldest first, for the conversation's own people
    (the same list rides the chat's socket as `session.join_requests`). Its owner adds
    them from the browser (`POST /{id}/participants`) or declines. Empty for anyone
    else."""
    session = _session_or_404(request, session_id)
    if not join_requests.may_see(request.user, session):
        return []
    return [_join_request_out(r) for r in join_requests.pending_for(session)]
