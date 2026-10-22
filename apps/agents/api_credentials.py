"""/api/agents/{slug}/… — an agent's secrets: credentials and their resolve, the
vault and zero-knowledge vault, secret requests, sender trust, GitHub and
Salesforce identities, device keys, credential alerts and PAT rotation."""
from __future__ import annotations

import datetime as dt
import uuid

from django.http import HttpRequest, HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_AUTH,
    TYPE_CONFLICT,
    TYPE_NOT_FOUND,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.events import audit
from apps.harness import device_keys
from apps.harness.api_device_keys import agents_key_opens, key_out, zk_agents_waiting_on
from apps.harness.channels.email import address_holder
from apps.harness.runners import may_trust_device_keys
from apps.harness.schemas import RunnerDeviceKeyOut
from apps.passkeys import stepup

from . import services
from .credentials import credential_resolve, delegations, pat_rotation, secret_requests, vault_alerts, zk, zk_endorse
from .credentials.zk import ZkError
from .gates import (
    agent_for_admin,
    agent_for_read,
)
from .models import (
    AgentCredential,
    AgentSecretRequest,
    SenderTrust,
    ZeroKnowledgeValue,
)
from .route_refusals import zk_problem
from .schemas import (
    AgentCredentialAccessOut,
    AgentCredentialAlertOut,
    AgentCredentialReadOut,
    AgentCredentialRevealOut,
    AgentCredentialsIn,
    AgentCredentialsResolveOut,
    AgentCredentialStatusOut,
    AgentGitHubIn,
    AgentGitHubOut,
    AgentSalesforceIn,
    AgentSalesforceOut,
    AgentVaultIn,
    AgentVaultOut,
    DeviceProofRequiredOut,
    PatRotationIn,
    PatRotationOut,
    SecretRequestIn,
    SecretRequestOut,
    SecretRequestValueIn,
    SenderTrustIn,
    SenderTrustOut,
    ZkEnableIn,
    ZkEndorsementsIn,
    ZkEndorsementsOut,
    ZkRecipientsOut,
    ZkRotateIn,
    ZkStateOut,
    ZkValuesIn,
    ZkValuesOut,
    ZkWrapMissingOut,
    ZkWrapsIn,
)

router = Router(auth=session_auth, tags=["agents"])


# ---- Agent credentials (spec 2026-09-05-agent-credentials-design) -----------

@router.put("/{slug}/credentials", response=list[AgentCredentialStatusOut],
            summary="Set named secrets for an agent (write-only)")
def set_agent_credentials(request: HttpRequest, slug: str, payload: AgentCredentialsIn):
    """Upsert. Non-clobbering: a ref absent from the body is untouched.

    The response is the MASKED status. From a browser session this needs a
    fresh passkey step-up (403 `step_up_required` otherwise); a token write —
    `canopy vault import` — does not, and is recorded like every settings change."""
    agent = agent_for_admin(request, slug)
    # Step-up only for a person at a browser (canopy-web#1538): a stolen session
    # cookie is the threat it closes. A PAT write has no browser to ask and is
    # audited as a SettingChange like any other; requiring a passkey there would
    # simply break `canopy vault import`.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)

    try:
        services.set_agent_credentials(agent, payload.values, user=request.user)
    except ZkError as exc:  # zero-knowledge, but nobody to seal to yet
        raise zk_problem(exc) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/status", response=list[AgentCredentialStatusOut],
            summary="Which declared refs are set (masked — booleans, never values)")
def agent_credential_status(request: HttpRequest, slug: str):
    """The question this answers is 'what is stopping this agent from running',
    which today requires SSH-ing to a box and reading a keyring."""
    agent = agent_for_read(request, slug)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/access", response=AgentCredentialAccessOut,
            summary="May the caller resolve this agent's secrets? (never values)")
def agent_credential_access(request: HttpRequest, slug: str) -> AgentCredentialAccessOut:
    """The cheap check `canopy cred` makes before fetching anything
    (canopy#850): which backend the agent resolves from, and whether THIS
    caller would be let through `/credentials/resolve` — and as whom.

    Session or bearer: it returns no values, so the browser may ask it too.
    A non-member gets 404, as everywhere else. `may_resolve` does not account
    for resolve's bearer-only rule, which is a property of the request rather
    than the caller."""
    agent = agent_for_read(request, slug)
    via, reason = credential_resolve.credential_access(request, agent)
    return AgentCredentialAccessOut(
        agent=agent.slug, credential_source=agent.credential_source,
        may_resolve=via is not None, via=via, reason=reason,
    )


@router.get("/{slug}/credentials/resolve",
            response={200: AgentCredentialsResolveOut, 401: DeviceProofRequiredOut,
                      409: ZkWrapMissingOut},
            summary="PLAINTEXT — a runner stages this agent's secrets")
def resolve_agent_credentials(request: HttpRequest, slug: str, refs: str | None = None):
    """The one route that returns values, and it is not for a browser.

    **Scoping (`?refs=a,b`).** Without `refs` the response is everything, as it
    always was. With `refs`, `values` holds only the named refs that the
    agent's runtime.yaml also declares, and the secret extras
    (`op_sa_token`, `shared_op_sa_token`, `github_token`, `salesforce_creds`)
    are included only when named in `refs` too. `op_vault`, `shared_op_vault`
    and `mailbox` are not secrets and are always returned.

    **`credential_source == "canopy-web"`** leaves out the agent's 1Password
    service-account token (`op_sa_token` is ""): its values come from here, so
    the box has no reason to hold a key to its vault.

    **Device proof.** With `credential_source` `canopy-web` and either
    `require_device_binding` or zero-knowledge on, the request must also carry a `DPoP` header: an RFC 9449
    proof (`typ: dpop+jwt`, `alg: ES256`, public `jwk` in the header; claims
    `htm`, `htu` without query, `iat` within ±120 s, single-use `jti`) signed by
    an ACTIVE runner device key on a runner this agent routes to or one its
    owner owns. Otherwise: 401 `{"error": "device_proof_required"}`.
    An EC2 cloud runner proves its instance role instead, in
    `X-Canopy-Aws-Proof`: base64 JSON `{method, url, headers, body}` of a
    SigV4-signed, unsent `sts:GetCallerIdentity` for `https://sts.amazonaws.com/`
    whose signed headers include `X-Canopy-Proof-Htu` (this URL) and
    `X-Canopy-Proof-Iat` (±120 s); single-use. canopy-web sends it to the pinned
    STS endpoint and accepts the returned role if an ACTIVE `aws-iam` device key
    for it sits on a runner this agent routes to.

    **Alerts.** With `credential_source` `canopy-web`, the agent's owner is told
    (email + push, at most once per kind per hour) of a box read (`via`
    runner) with no `X-Canopy-Turn-Id`, a read of an undeclared ref, a burst of
    reads, or a refusal for a bad or missing device proof.

    **Read log.** Every ref handed out is recorded with the caller, the runner
    when known (the proving device key's runner, else `X-Canopy-Runner: <runner
    id>`, honoured only for a runner the caller owns) and `X-Canopy-Turn-Id`.

    Two gates, both required:

    1. **Bearer only.** A session cookie is refused even for the owner. That is
       what makes "the browser never sees plaintext" a property of the system
       rather than a habit of the UI — a future page cannot accidentally acquire
       the ability to render a secret.
    2. **The caller must be one of three principals** (`credential_resolve.credential_access`):
       - `runner`: pairs a live runner this agent routes to. Mirrors the runner
         credential fetch, whose boundary is "the caller who can claim turns as
         this runner".
       - `admin`: is the agent's owner or an admin (`Agent.is_admin`) — a human
         resolving for a local Claude Code session (canopy#850). Not a widening
         in substance: an admin can already write every value, swap the vault
         key, and put their own box on the agent to read them back.
       - `license`: holds an ACTIVE license to use the agent locally and names
         their OWN open local turn on it in `X-Canopy-Turn-Id` (canopy-web#1704).
         The read is logged against that turn and license.
       Anyone else — a member, an editor with no local turn — is refused, and
       told which access to ask for. Tighter than workspace membership on purpose.

    Every read is recorded, so a credential fetch is visible in the fleet log
    rather than silent.
    """
    agent = agent_for_read(request, slug)
    result = credential_resolve.resolve(request, agent, refs)
    if isinstance(result, credential_resolve.Refusal):
        raise _credential_refusal(result)
    return AgentCredentialsResolveOut(**result.response_fields())


def _credential_refusal(refusal: credential_resolve.Refusal) -> Exception:
    """The HTTP answer to a refused resolve (`credential_resolve.Refusal`)."""
    cr = credential_resolve
    if refusal.code == cr.DEVICE_PROOF_REQUIRED:
        return ProblemError(401, "Device proof required", type_=TYPE_AUTH, detail=refusal.detail,
                            members={"error": "device_proof_required", "reason": refusal.reason},
                            headers={"WWW-Authenticate": 'DPoP algs="ES256"'})
    if refusal.code == cr.ZK_WRAP_MISSING:
        return ProblemError(409, "Vault key not granted", type_=TYPE_CONFLICT, detail=refusal.detail,
                            members={"error": "zk_wrap_missing", "kv": refusal.kv,
                                     "device_key": refusal.device_key})
    # BEARER_REQUIRED, NO_ACCESS, FOREIGN_DEVICE_PROOF
    return HttpError(403, refusal.detail)


@router.get("/{slug}/vault", response=AgentVaultOut,
            summary="This agent's 1Password vault (masked — never the key)")
def get_agent_vault(request: HttpRequest, slug: str) -> AgentVaultOut:
    agent = agent_for_read(request, slug)
    return services.agent_vault_status(agent)


@router.put("/{slug}/vault", response=AgentVaultOut,
            summary="Set the vault + its service-account token (write-only)")
def set_agent_vault(request: HttpRequest, slug: str, payload: AgentVaultIn) -> AgentVaultOut:
    """The key is scoped to ONE agent's vault by design.

    A single fleet-wide token would be simpler to operate and would make
    canopy-web worth attacking for every agent's secrets at once; this bounds a
    compromise to the one agent whose key was taken (Jonathan, 2026-09-06)."""
    agent = agent_for_admin(request, slug)
    return services.set_agent_vault(
        agent, vault=payload.vault, service_key=payload.service_key,
    )


# ---- zero-knowledge vault (Canopy Vault Phase 2, canopy-web#1550) ------------------
# Formats: docs/vault/zk-v1/zk_v1_reference.py. canopy-web only shape-checks and
# stores; the browser and the canopy CLI do every encryption (apps/agents/credentials/zk.py).
# Owner/admin only. Writes from a browser need a passkey step-up; a token held
# by an admin may enable, set values, grant and rotate (the canopy CLI acts as
# its device) — but turning ZK OFF hands plaintext back, so it is browser-only.


def _zk_admin(request: HttpRequest, slug: str, *, browser_only: bool = False):
    agent = agent_for_admin(request, slug)
    if browser_only or stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    return agent


def _zk_state(agent, *, include_values: bool = False) -> ZkStateOut:

    return ZkStateOut(**zk.state(agent, include_values=include_values))


@router.get("/{slug}/vault/zk", response=ZkStateOut,
            summary="Zero-knowledge state: kv, wraps, and who still needs one")
def get_agent_zk_state(request: HttpRequest, slug: str, values: bool = False) -> ZkStateOut:
    """`pending` lists active device keys (with an ECDH key) on runners this
    agent's resolve accepts, and current agent admins with a vault key, that
    hold no wrap at the current `kv` — each with the public key to wrap to.
    `wraps` are public data. `?values=true` adds the `zk1:` ciphertexts (what
    rotate and turn-off need), and records a read of each."""
    agent = agent_for_admin(request, slug)
    out = _zk_state(agent, include_values=values)
    if values and out.values:
        services.record_credential_reads(agent, list(out.values), user=request.user, via="zk-state")
    return out


@router.post("/{slug}/vault/zk/enable", response=ZkStateOut,
             summary="Turn on zero-knowledge: wraps + every value as ciphertext, atomically")
def enable_agent_zk(request: HttpRequest, slug: str, payload: ZkEnableIn) -> ZkStateOut:
    """`{kv: 1, wraps: [{kind, target, wrap}], values: [{name, value_zk}]}`.
    Needs exactly one `recovery` wrap and a wrap the caller can open (their
    member wrap, or a device wrap on a runner they own — the CLI enabling as
    its device; the owner's member wrap can be granted later). `values` must cover every stored credential; each one's
    custodial copy is wiped. Errors are problem+json with an `error` code."""

    agent = _zk_admin(request, slug)
    try:
        zk.enable(agent, kv=payload.kv, wraps=payload.wraps, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


@router.put("/{slug}/vault/zk/values", response=ZkValuesOut,
            summary="Set zero-knowledge values (ciphertext) at the current kv")
def set_agent_zk_values(request: HttpRequest, slug: str, payload: ZkValuesIn) -> ZkValuesOut:
    """Non-clobbering upsert of `{name, value_zk}`. A `kv` other than the
    current one is 409 `zk_kv_mismatch` (the vault key was rotated)."""

    agent = _zk_admin(request, slug)
    try:
        names = zk.set_values(agent, kv=payload.kv, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return ZkValuesOut(kv=payload.kv, names=names)


@router.post("/{slug}/vault/zk/wraps", response=ZkStateOut,
             summary="Grant: wrap the vault key to pending devices or admins")
def add_agent_zk_wraps(request: HttpRequest, slug: str, payload: ZkWrapsIn) -> ZkStateOut:
    """`{kv, wraps: [{kind: device|member, target, wrap}]}`. A device target is
    an ACTIVE key with an ECDH key on a runner this agent routes to; a member
    target is a current agent admin with a vault key. Replaces that target's
    wrap at this kv. The recovery wrap is set only by enable and rotate."""

    agent = _zk_admin(request, slug)
    try:
        zk.add_wraps(agent, kv=payload.kv, wraps=payload.wraps, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return _zk_state(agent)


@router.post("/{slug}/vault/zk/rotate", response=ZkStateOut,
             summary="Rotate the vault key: replace every wrap and value atomically")
def rotate_agent_zk(request: HttpRequest, slug: str, payload: ZkRotateIn) -> ZkStateOut:
    """`{new_kv: kv + 1, wraps, values}`. Every target holding a wrap now that
    still qualifies must get one at `new_kv` (422 `zk_rewrap_required`
    otherwise); a revoked device or removed admin is simply left out. A fresh
    recovery wrap and a wrap the caller can open are required, and `values`
    must cover every stored credential."""

    agent = _zk_admin(request, slug)
    try:
        zk.rotate(agent, new_kv=payload.new_kv, wraps=payload.wraps, values=payload.values,
                  user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    agent.refresh_from_db()
    return _zk_state(agent)


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
def request_agent_secret(request: HttpRequest, slug: str, payload: SecretRequestIn) -> SecretRequestOut:
    """For the agent itself (its own login) or an admin. `name` must be declared
    in the agent's runtime.yaml. The chat this is called from shows the person a
    card that seals the value in their browser and stores ciphertext only — you
    never receive the value, and must not ask for it in the chat. Read the
    request back (`get_agent_secret_request`) for `status: fulfilled`, then
    resolve the secret the usual way."""

    agent = agent_for_read(request, slug)
    try:
        req = secret_requests.create(agent, name=payload.name, reason=payload.reason, user=request.user)
    except secret_requests.RequestError as exc:
        raise _secret_request_problem(exc) from exc
    where = ("in this chat" if req.session_id else
             f"on {agent.slug}'s Credentials page (this call was not made from one of its chats)")
    return _secret_request_out(req, message=(
        f"Asked for {req.name}: a card to enter it is shown {where}. Its value is sealed in the "
        "person's browser and never reaches you or canopy. Do not ask for it in the conversation."))


@router.get("/{slug}/secret-requests/{request_id}", response=SecretRequestOut,
            summary="A secret request's status (never its value)")
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
    if not agent.zk_enabled:
        raise zk_problem(zk_endorse.ZkError(409, "zk_not_enabled", f"{agent.slug} is not zero-knowledge"))
    return ZkRecipientsOut(
        agent=agent.slug, agent_id=str(agent.pk), kv=agent.zk_kv,
        recipients=zk_endorse.recipients(agent), endorsements=zk_endorse.endorsements(agent),
        declared=sorted(services.declared_credential_names(agent)),
        stored=sorted(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True)))


@router.post("/{slug}/vault/zk/endorsements", response=ZkEndorsementsOut,
             summary="Vouch, with your own vault key, that keys may read this agent")
def add_agent_zk_endorsements(request: HttpRequest, slug: str, payload: ZkEndorsementsIn) -> ZkEndorsementsOut:
    """`{endorsements: [{kind, target, key_jkt, by_jkt, sig}]}` — each a zk-v1
    §7 signature by YOUR vault key over a CURRENT recipient's current key. The
    browser makes them whenever you wrap the vault key to someone (turn on,
    grant, rotate) or confirm who can read the agent."""

    agent = _zk_admin(request, slug)
    try:
        n = zk_endorse.add(agent, user=request.user, items=payload.endorsements)
    except zk_endorse.ZkError as exc:
        raise zk_problem(exc) from exc
    return ZkEndorsementsOut(stored=n)


# ---- sender trust: a temporary exception to email alignment ----------------------
# See agents.models.SenderTrust. Agent-admin to write: it decides who an email
# is, and so what the agent's full profile answers to.

def _sender_trust_out(rule) -> SenderTrustOut:
    from django.utils import timezone

    return SenderTrustOut(
        email=rule.email, reason=rule.reason, expires_at=rule.expires_at,
        active=rule.expires_at > timezone.now(),
        created_by=rule.created_by.email if rule.created_by_id else None,
        created_at=rule.created_at,
    )


@router.get("/{slug}/sender-trust", response=list[SenderTrustOut],
            summary="List temporary trust rules for members' unaligned email")
def list_sender_trust(request: HttpRequest, slug: str) -> list[SenderTrustOut]:
    """Addresses whose mail is tied to their member account although it is not
    DMARC- or DKIM-aligned — each with why, and when it lapses (expired rules
    are listed with `active: false` until removed)."""

    agent = agent_for_read(request, slug)
    return [_sender_trust_out(r) for r in
            SenderTrust.objects.filter(agent=agent).select_related("created_by")]


@router.put("/{slug}/sender-trust/{email}", response=SenderTrustOut,
            summary="Temporarily trust one member's unaligned email")
def set_sender_trust(request: HttpRequest, slug: str, email: str,
                     payload: SenderTrustIn) -> SenderTrustOut:
    """For `days` (1–90), mail From: this exact address is tied to its member
    account although it is not DMARC- or DKIM-aligned — as long as it still
    carries a passing DKIM signature, and the address still belongs to exactly
    one member of the agent's workspace. For a member whose domain signs with
    its provider's key (no DMARC, no own-domain DKIM) while that is fixed.
    Re-PUT to extend or reword; DELETE to end it early."""
    from django.utils import timezone

    agent = agent_for_admin(request, slug)
    address = (email or "").strip().lower()
    if "@" not in address:
        raise HttpError(422, f"not an email address: {email!r}")
    user, role = address_holder(address, agent.workspace_id)
    if user is None or role is None:
        raise HttpError(422, f"{address} does not belong to a member of this agent's "
                             "workspace; a trust rule only ties mail to a member")
    rule, _ = SenderTrust.objects.update_or_create(
        agent=agent, email=address,
        defaults={"reason": payload.reason.strip(),
                  "expires_at": timezone.now() + dt.timedelta(days=payload.days),
                  "created_by": request.user},
    )
    return _sender_trust_out(rule)


@router.delete("/{slug}/sender-trust/{email}", response={204: None},
               summary="End a temporary email trust rule")
def delete_sender_trust(request: HttpRequest, slug: str, email: str):
    """Their mail goes back to needing DMARC or own-domain DKIM. Idempotent."""

    agent = agent_for_admin(request, slug)
    SenderTrust.objects.filter(agent=agent, email=(email or "").strip().lower()).delete()
    return Status(204, None)


# ---- GitHub: the owner's (or an admin's) identity, lent to this agent ---------
# The owner or an admin pastes a fine-grained token here; a runner is handed it one turn at a
# time. See apps/agents/credentials/delegations.py for the rule and AgentDelegation for why it
# is not a vault item.

@router.get("/{slug}/github", response=AgentGitHubOut,
            summary="How this agent acts on GitHub (masked — never the token)")
def get_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Whose GitHub identity this agent's pull requests use, when that token
    expires, and whether it can open a pull request on the agent's repo."""
    agent = agent_for_read(request, slug)
    return _github_out(request, agent)


def _github_out(request: HttpRequest, agent) -> AgentGitHubOut:
    return AgentGitHubOut(**delegations.status(agent), can_lend=agent.is_admin(request.user))


@router.put("/{slug}/github", response=AgentGitHubOut,
            summary="Lend this agent your GitHub identity (owner or admin, write-only)")
def set_agent_github(request: HttpRequest, slug: str, payload: AgentGitHubIn) -> AgentGitHubOut:
    """Store the caller's fine-grained GitHub token for this agent — its owner's,
    or an admin's (used while the owner has lent none).

    Checked before it is stored: GitHub must accept it, and it must be able to
    open a pull request on the agent's own repo. A token that fails is refused
    with the reason, never saved."""
    agent = agent_for_read(request, slug)
    try:
        delegations.set_github(agent, request.user, payload.token)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    return _github_out(request, agent)


@router.post("/{slug}/github/check", response=AgentGitHubOut,
             summary="Re-check this agent's GitHub token against GitHub")
def check_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    agent = agent_for_read(request, slug)
    delegations.check_github(agent)
    return _github_out(request, agent)


@router.delete("/{slug}/github", response=AgentGitHubOut,
               summary="Withdraw your GitHub identity from this agent")
def delete_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Removes the CALLER's own delegation — nobody can withdraw someone
    else's."""
    agent = agent_for_read(request, slug)
    delegations.clear_github(agent, request.user)
    return _github_out(request, agent)


# ---- Salesforce: another agent's own identity, lent to this one ---------------
# chrome-sales acts in Salesforce as one delegated identity (Eva's SF user). The
# row names the lender; the credential is the lender's own, resolved at use time.
# See the Salesforce section of apps/agents/credentials/delegations.py.

@router.get("/{slug}/salesforce", response=AgentSalesforceOut,
            summary="Whose Salesforce identity this agent borrows (masked — never the credential)")
def get_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.put("/{slug}/salesforce", response=AgentSalesforceOut,
            summary="Lend this agent another agent's Salesforce identity (owner of both)")
def set_agent_salesforce(request: HttpRequest, slug: str,
                         payload: AgentSalesforceIn) -> AgentSalesforceOut:
    """Checked before it is stored: the lender must hold a Salesforce credential
    and Salesforce must accept it. Refused with the reason otherwise."""
    agent = agent_for_read(request, slug)
    try:
        delegations.set_salesforce(agent, request.user, payload.lender)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.post("/{slug}/salesforce/check", response=AgentSalesforceOut,
             summary="Re-check the Salesforce identity this agent borrows")
def check_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    delegations.check_salesforce(agent)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.delete("/{slug}/salesforce", response=AgentSalesforceOut,
               summary="Stop lending this agent a Salesforce identity")
def delete_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    delegations.clear_salesforce(agent, request.user)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


# Registered AFTER the literal `status`/`resolve` paths on purpose: Django
# resolves in order, so a `{name}` pattern declared first swallows "status" and
# answers 405 Method Not Allowed — the route exists, it is simply unreachable.
@router.delete("/{slug}/credentials/{name}", response=list[AgentCredentialStatusOut],
               summary="Remove one named secret")
def delete_agent_credential(request: HttpRequest, slug: str, name: str):
    agent = agent_for_admin(request, slug)
    services.delete_agent_credential(agent, name)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/{name}/reads", response=list[AgentCredentialReadOut],
            summary="Who read this credential's value, from which box and turn")
def agent_credential_reads(request: HttpRequest, slug: str, name: str, limit: int = 100):
    """The read log (canopy-web#1538), newest first. Never the value. The
    agent's owner or an admin — the same people who may set the value."""
    agent = agent_for_admin(request, slug)
    return services.credential_reads(agent, name, limit=max(1, min(limit, 500)))


@router.get("/{slug}/device-keys", response=list[RunnerDeviceKeyOut],
            summary="Runner device keys that could prove themselves for this agent")
def agent_device_keys(request: HttpRequest, slug: str):
    """Every device key on a runner this agent routes to or its owner owns —
    the keys `require_device_binding` would accept once active. `can_manage`
    says whether the caller may approve each (the runner's owner or an admin of
    it; on a private runner, its owner alone)."""

    agent = agent_for_admin(request, slug)
    vault = [agent] if agent.zk_enabled and agent.credential_source == "canopy-web" else []
    out = []
    for k in device_keys.keys_for_agent(agent):
        row = key_out(k, can_manage=may_trust_device_keys(request.user, k.runner))
        row.can_open = agents_key_opens(k, vault)
        if row.can_manage:
            # Approving it will also wrap these (one passkey touch, canopy-web#1550);
            # an approved key lists what it still waits on ("Give access").
            row.zk_pending_agents = zk_agents_waiting_on(request.user, k, assume_active=not k.ecdh_ready)
        out.append(row)
    return out


@router.get("/{slug}/credential-alerts", response=list[AgentCredentialAlertOut],
            summary="Read-anomaly alerts on this agent's secrets")
def agent_credential_alerts(request: HttpRequest, slug: str, limit: int = 50):
    """Canopy Vault (canopy-web#1549), newest first: reads with no turn id by
    someone not the owner or an admin, reads of undeclared refs, bursts, and
    resolves refused for a bad or missing device proof. At most one per kind
    per hour (`occurrences` counts the rest). The agent's owner or an admin."""

    agent = agent_for_admin(request, slug)
    return vault_alerts.alerts_for(agent, limit=max(1, min(limit, 200)))


@router.get("/{slug}/pat-rotation", response=PatRotationOut,
            summary="This agent's canopy-pat rotation: setting, next due, history")
def get_pat_rotation(request: HttpRequest, slug: str) -> PatRotationOut:
    """Never a token. The agent's owner or an admin."""

    agent = agent_for_admin(request, slug)
    return PatRotationOut(**pat_rotation.status(agent))


@router.patch("/{slug}/pat-rotation", response=PatRotationOut,
              summary="Turn automatic canopy-pat rotation on or off")
def set_pat_rotation(request: HttpRequest, slug: str, payload: PatRotationIn) -> PatRotationOut:
    """Canopy Vault (canopy-web#1549). When on, every `period_days` (default
    30) canopy-web mints a new token for the agent's own canopy login, writes
    it to the agent's `canopy-pat` credential (a runner picks it up at its next
    `canopy cred sync`), keeps the old token valid for 24 h and then revokes it.
    Turning it on needs the agent to have its own login and a stored
    `canopy-pat`, and — when that login has ever signed in interactively — a
    workspace owner (422/403 otherwise). The owner or an admin."""

    agent = agent_for_admin(request, slug)
    before = (agent.pat_rotation_enabled, agent.pat_rotation_days)
    if payload.enabled:
        refusal = pat_rotation.enable_refusal(agent, request.user)
        if refusal:
            raise HttpError(refusal.status, str(refusal))
        if not agent.pat_rotation_enabled:
            agent.pat_rotation_enabled_by = request.user
    agent.pat_rotation_enabled = payload.enabled
    agent.pat_rotation_days = payload.period_days
    agent.save(update_fields=["pat_rotation_enabled", "pat_rotation_days",
                              "pat_rotation_enabled_by", "updated_at"])
    audit.note_change(scope=f"agent:{agent.slug}/pat-rotation",
                      before=f"{'on' if before[0] else 'off'}/{before[1]}d",
                      after=f"{'on' if payload.enabled else 'off'}/{payload.period_days}d")
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/pat-rotation/rotate", response=PatRotationOut,
             summary="Rotate this agent's canopy-pat now")
def rotate_pat_now(request: HttpRequest, slug: str) -> PatRotationOut:
    """The same rotation the schedule does, now, whether or not rotation is on:
    a new token in `canopy-pat`, the old one valid for 24 h then revoked. It
    changes a secret, so from a browser it needs a fresh passkey step-up (403
    `step_up_required`); a token may call it. The owner or an admin."""

    agent = agent_for_admin(request, slug)
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        pat_rotation.rotate(agent, trigger="manual", actor=request.user)
    except pat_rotation.RotationRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    agent.refresh_from_db()
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/credentials/{name}/reveal", response=AgentCredentialRevealOut,
             summary="PLAINTEXT — show one stored value to an admin who just confirmed a passkey")
def reveal_agent_credential(request: HttpRequest, response: HttpResponse, slug: str, name: str):
    """Owner/agent admin, at a browser, with a passkey step-up in the last five
    minutes (403 `step_up_required` otherwise). Returns the value once and
    records who saw it; tokens are refused — a machine reads values through
    `/credentials/resolve`."""
    # The one deliberate exception to "a browser never sees plaintext"
    # (canopy-web#1538). What makes it safe is the conjunction: agent admin (who
    # could already overwrite the value and route a box of their own to read it)
    # AND a person present at the browser (step-up) AND a durable record.

    agent = agent_for_admin(request, slug)
    stepup.require_recent_step_up(request)
    try:
        value = services.reveal_agent_credential(agent, name, user=request.user)
    except ZeroKnowledgeValue as exc:
        raise ProblemError(409, "Zero-knowledge value", type_=TYPE_CONFLICT, detail=str(exc),
                           members={"error": "zk_enabled"}) from exc
    if value is None:
        raise ProblemError(404, "Credential not found", type_=TYPE_NOT_FOUND,
                           detail=f"{agent.slug} has no value stored under {name!r}")
    response["Cache-Control"] = "no-store"
    return AgentCredentialRevealOut(name=name, value=value)
