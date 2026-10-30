"""/api/agents/{slug}/secret-requests and the recipients/endorsements a person's browser seals against —
giving an agent a secret (`docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md`).
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_CONFLICT,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.api.route_gates import gate
from apps.passkeys import stepup

from . import services
from .credentials import (
    secret_requests,
    vault_members,
    zk_endorse,
)
from .gates import (
    agent_for_admin,
    agent_for_read,
)
from .models import (
    AgentCredential,
    AgentSecretRequest,
)
from .route_refusals import zk_problem
from .schemas import (
    SecretRequestIn,
    SecretRequestOut,
    SecretRequestValueIn,
    ZkEndorsementsIn,
    ZkEndorsementsOut,
    ZkRecipientsOut,
)
from .vault_gates import zk_admin

router = Router(auth=session_auth, tags=["agents"])

# ---- give an agent a secret: requests, recipients, endorsements ---------------------
# Spec: docs/superpowers/specs/2026-10-19-give-an-agent-a-secret-design.md. The
# agent asks; a person's browser seals the value (zks1) to recipients endorsed by
# a key it trusts, and sends ciphertext only. No route here returns a value.

def _secret_request_problem(exc) -> ProblemError:
    return ProblemError(exc.status, "Secret request", type_=(
        TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION), detail=str(exc),
        members={"error": exc.code})


def _secret_request(request: HttpRequest, slug: str, request_id: uuid.UUID):

    agent = agent_for_read(request, slug)
    req = (AgentSecretRequest.objects.filter(pk=request_id, agent=agent)
           .select_related("agent__workspace", "session", "fulfilled_by").first())
    if req is None:
        raise HttpError(404, "no such secret request")
    return req


def _secret_request_out(req, message: str = "") -> SecretRequestOut:

    data = secret_requests.out(req)
    return SecretRequestOut(**data, secret_request_id=data["id"], message=message)


@router.post("/{slug}/secret-requests", response=SecretRequestOut,
             summary="Ask a person for one of this agent's declared secrets")
# give an agent a secret (apps/agents/credentials/secret_requests.py): no route returns a value
# declared names only
# an agent asking a person for a declared secret: carries no value; the
# answer (fulfil_agent_secret_request) is the setting
@gate("agent-login", "agent-admin", kind="request")
def request_agent_secret(request: HttpRequest, slug: str, payload: SecretRequestIn) -> SecretRequestOut:
    """For the agent itself (its own login) or an admin. `name` must be declared
    in the agent's vault (`runtime_secrets`). The chat this is called from shows the person a
    card that seals the value in their browser and stores ciphertext only — you
    never receive the value, and must not ask for it in the chat. Read the
    request back (`get_agent_secret_request`) for `status: fulfilled`, then
    resolve the secret the usual way. Refused in a contact's chat (409
    `contact_chat`): a contact can't fill a secret, so ask the agent's owner."""

    agent = agent_for_read(request, slug)
    try:
        req = secret_requests.create(agent, name=payload.name, reason=payload.reason, user=request.user)
    except secret_requests.RequestError as exc:
        raise _secret_request_problem(exc) from exc
    where = ("in this chat" if req.session_id else
             f"on {agent.slug}'s Security tab (this call was not made from one of its chats)")
    return _secret_request_out(req, message=(
        f"Asked for {req.name}: a card to enter it is shown {where}. Its value is sealed in the "
        "person's browser and never reaches you or canopy. Do not ask for it in the conversation."))


@router.get("/{slug}/secret-requests", response=list[SecretRequestOut],
            summary="This agent's open secret requests (never a value)")
@gate("agent-admin")  # names and reasons, never a value
def list_agent_secret_requests(request: HttpRequest, slug: str) -> list[SecretRequestOut]:
    """The requests still waiting for an answer, newest first — what the agent's
    Security tab shows its admins, including a request made outside any of
    its chats (`session_id` null), which no chat card carries."""

    agent = agent_for_admin(request, slug)
    return [_secret_request_out(r) for r in secret_requests.open_requests(agent)]


@router.get("/{slug}/secret-requests/{request_id}", response=SecretRequestOut,
            summary="A secret request's status (never its value)")
@gate("agent-login", "agent-admin", "session-acl")  # status, never a value
def get_agent_secret_request(request: HttpRequest, slug: str, request_id: uuid.UUID) -> SecretRequestOut:
    """`status` is open, fulfilled, cancelled or expired; `has_value` says
    whether the agent now holds a value for the name. For the agent, its admins
    and whoever can read the chat it was asked from."""

    req = _secret_request(request, slug, request_id)
    if not (secret_requests.may_read(req, request.user) or req.agent.is_own_login(request.user)):
        raise HttpError(404, "no such secret request")
    return _secret_request_out(req)


@router.put("/{slug}/secret-requests/{request_id}/value", response=SecretRequestOut,
            summary="Answer a secret request with a value sealed in the browser (zks1)")
@gate("agent-admin", "secret-request", setting=True, step_up=True)  # zks1 only; browser: + step-up
def fulfil_agent_secret_request(request: HttpRequest, slug: str, request_id: uuid.UUID,
                                payload: SecretRequestValueIn) -> SecretRequestOut:
    """`value_zk` must be `zks1:` — sealed to the agent's readers before it left
    the page — at the agent's current `kv`. An admin may fill or replace; a
    person who can write to the request's chat may fill a MISSING value only
    (409 `value_exists`). An agent's own login can never answer. A browser
    needs a passkey step-up."""

    req = _secret_request(request, slug, request_id)
    if not secret_requests.may_answer(req, request.user):
        raise HttpError(403, "you cannot answer this request")
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        secret_requests.fulfil(req, kv=payload.kv, value_zk=payload.value_zk, user=request.user)
    except secret_requests.RequestError as exc:
        raise _secret_request_problem(exc) from exc
    req.refresh_from_db()
    return _secret_request_out(req, message=f"{req.name} is stored for {req.agent.slug}.")


@router.get("/{slug}/vault/zk/recipients", response=ZkRecipientsOut,
            summary="Who a sealed value is for, and who vouched for each (public keys only)")
@gate("agent-admin", "secret-request")  # public keys + endorsements
def get_agent_zk_recipients(request: HttpRequest, slug: str,
                            secret_request: uuid.UUID | None = None) -> ZkRecipientsOut:
    """For an admin, or for someone who may answer the open request named by
    `secret_request`. Verify each recipient's endorsement against a key you
    already trust BEFORE sealing to it (zk-v1 §7): this list is canopy-web's
    claim, the endorsements are not."""

    agent = agent_for_read(request, slug)
    if not agent.is_admin(request.user):
        req = _secret_request(request, slug, secret_request) if secret_request else None
        if req is None or not secret_requests.live(req) or not secret_requests.may_answer(req, request.user):
            raise HttpError(403, "only the agent's admins, or someone answering its open request")
    return ZkRecipientsOut(
        agent=agent.slug, agent_id=str(agent.pk), kv=agent.zk_kv,
        recipients=zk_endorse.recipients(agent), endorsements=zk_endorse.endorsements(agent),
        declared=sorted(services.declared_credential_names(agent)),
        stored=sorted(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True)))


@router.post("/{slug}/vault/zk/endorsements", response=ZkEndorsementsOut,
             summary="Vouch, with your own vault key, that keys may read this agent")
# own vault key's signatures; browser: + step-up. self: a member of the vault who does
# not manage it, vouching for their OWN machine only.
@gate("agent-admin", "vault-admin", "self", setting=True, step_up=True)
def add_agent_zk_endorsements(request: HttpRequest, slug: str, payload: ZkEndorsementsIn) -> ZkEndorsementsOut:
    """`{endorsements: [{kind, target, key_jkt, by_jkt, sig}]}` — each a zk-v1
    §7 signature by YOUR vault key over a CURRENT recipient's current key. The
    browser makes them whenever you wrap the vault key to someone (turn on,
    grant, rotate) or confirm who can read the agent — and when a member of the
    vault approves their own machine, which a keeper then grants (they may vouch
    for nothing else)."""

    agent = agent_for_read(request, slug)
    role = vault_members.role_of(vault_members.vault_for_agent(agent), request.user)
    manager = agent.is_admin(request.user) and vault_members.is_admin(role)
    if manager:
        agent = zk_admin(request, slug)
    elif role is None:
        raise HttpError(403, "this action requires a member of this agent's vault")
    elif stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        n = zk_endorse.add(agent, user=request.user, items=payload.endorsements,
                           own_machines_only=not manager)
    except zk_endorse.ZkError as exc:
        raise zk_problem(exc) from exc
    return ZkEndorsementsOut(stored=n)


