"""/api/agents/{slug}/… — an agent's secrets: credentials and their resolve, the
vault and zero-knowledge vault, secret requests, sender trust, GitHub and
Salesforce identities, device keys, credential alerts and PAT rotation."""
from __future__ import annotations

import datetime as dt
import uuid

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_AUTH,
    TYPE_CONFLICT,
    TYPE_FORBIDDEN,
    TYPE_NOT_FOUND,
    TYPE_VALIDATION,
    ProblemError,
)
from apps.api.route_gates import gate
from apps.events import audit
from apps.harness import device_keys
from apps.harness.api_device_keys import agents_key_opens, key_out, zk_agents_waiting_on
from apps.harness.channels.email import address_holder
from apps.harness.runners import may_trust_device_keys
from apps.harness.schemas import RunnerDeviceKeyOut
from apps.passkeys import stepup
from apps.passkeys.models import UserVaultKeyset
from apps.tokens import kinds as token_kinds

from . import services
from .credentials import (
    credential_resolve,
    delegations,
    pat_rotation,
    secret_requests,
    vault_alerts,
    vault_keeper,
    vault_members,
    zk,
    zk_endorse,
)
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
    OwnPatRotateIn,
    OwnPatRotateOut,
    PatRotationIn,
    PatRotationOut,
    SecretRequestIn,
    SecretRequestOut,
    SecretRequestValueIn,
    SenderTrustIn,
    SenderTrustOut,
    VaultAgentLinkIn,
    VaultKeeperPinsIn,
    VaultKeeperPinsOut,
    VaultMemberIn,
    VaultMembersOut,
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
@gate("agent-admin", setting=True, step_up=True)
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
    # Storing a value is declaring it: the caller is the agent's owner or admin.
    services.declare_credential_names(agent, payload.values)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/status", response=list[AgentCredentialStatusOut],
            summary="Which declared refs are set (masked — booleans, never values)")
@gate("member")
def agent_credential_status(request: HttpRequest, slug: str):
    """The question this answers is 'what is stopping this agent from running',
    which today requires SSH-ing to a box and reading a keyring."""
    agent = agent_for_read(request, slug)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/access", response=AgentCredentialAccessOut,
            summary="May the caller resolve this agent's secrets? (never values)")
@gate("member")  # no values; reports the resolve gate for the caller
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
@gate("runner-holds-agent", "agent-admin", "license", step_up="runner protocol")  # bearer only
def resolve_agent_credentials(request: HttpRequest, slug: str, refs: str | None = None):
    """The one route that returns values, and it is not for a browser.

    **Scoping (`?refs=a,b`).** Without `refs` the response is everything, as it
    always was. With `refs`, `values` holds only the named refs that the
    agent also declares (`runtime_secrets`), and the secret extras
    (`op_sa_token`, `shared_op_sa_token`, `salesforce_creds`) are included
    only when named in `refs` too. `op_vault`, `shared_op_vault` and `mailbox`
    are not secrets and are always returned.

    **Never the agent's GitHub token.** That borrowed credential (the token its owner
    or an admin gave it from their own GitHub account) leaves canopy-web only for one turn a runner holds
    (`POST /api/harness/runners/{id}/turns/{id}/github-token`), never from here.

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
    if refusal.code == cr.NOT_A_VAULT_MEMBER:
        return ProblemError(403, "Not a vault member", type_=TYPE_AUTH, detail=refusal.detail,
                            members={"error": "not_a_vault_member"})
    # BEARER_REQUIRED, NO_ACCESS, FOREIGN_DEVICE_PROOF
    return HttpError(403, refusal.detail)


@router.get("/{slug}/vault", response=AgentVaultOut,
            summary="This agent's 1Password vault (masked — never the key)")
@gate("member")
def get_agent_vault(request: HttpRequest, slug: str) -> AgentVaultOut:
    agent = agent_for_read(request, slug)
    return services.agent_vault_status(agent)


@router.put("/{slug}/vault", response=AgentVaultOut,
            summary="Set the vault + its service-account token (write-only)")
@gate("agent-admin", setting=True, step_up=True)
def set_agent_vault(request: HttpRequest, slug: str, payload: AgentVaultIn) -> AgentVaultOut:
    """The key is scoped to ONE agent's vault by design.

    A single fleet-wide token would be simpler to operate and would make
    canopy-web worth attacking for every agent's secrets at once; this bounds a
    compromise to the one agent whose key was taken (Jonathan, 2026-09-06)."""
    agent = agent_for_admin(request, slug)
    # A service-account key is a secret: a browser needs a fresh passkey step-up,
    # as `set_agent_credentials` does; a token write has no browser to ask.
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
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
    # The agent's admin AND an owner or admin of its vault, as the workspace vault's
    # writes are: someone taken out of an unlinked vault must not be able to rotate
    # its key to one of their own machines and read what is written after.
    if not vault_members.is_admin(vault_members.role_of(vault_members.vault_for_agent(agent), request.user)):
        raise HttpError(403, "this action requires someone who manages this agent's vault")
    if browser_only or stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    return agent


def _zk_state(agent, *, include_values: bool = False) -> ZkStateOut:

    return ZkStateOut(**zk.state(agent, include_values=include_values),
                      pins=vault_keeper.pins_of(vault_members.vault_for_agent(agent)))


@router.get("/{slug}/vault/zk", response=ZkStateOut,
            summary="Zero-knowledge state: kv, wraps, and who still needs one")
# zero-knowledge vault (apps/agents/credentials/zk.py): browser writes need a passkey step-up
# wraps are public; ?values= ciphertext, read-logged
@gate("agent-admin", step_up="zk1: ciphertext only, which canopy-web cannot open")
def get_agent_zk_state(request: HttpRequest, slug: str, values: bool = False) -> ZkStateOut:
    """`pending` lists active device keys (with an ECDH key) on runners this
    agent's resolve accepts, and current agent admins with a vault key, that
    hold no wrap at the current `kv` — each with the public key to wrap to.
    `wraps` are public data. `?values=true` adds the `zk1:` ciphertexts (what
    rotate and turn-off need), and records a read of each."""
    agent = agent_for_admin(request, slug)
    if values and request.user.pk not in vault_members.agent_member_ids(agent):
        # An agent admin who is not a member of its vault (the link was removed)
        # may still hold an old copy of the key: the ciphertext goes to members only.
        raise ProblemError(403, "Not a vault member", type_=TYPE_FORBIDDEN,
                           detail=f"You are not a member of {agent.slug}'s vault; a manager of the vault must add you.",
                           members={"error": "not_a_vault_member"})
    out = _zk_state(agent, include_values=values)
    if values and out.values:
        services.record_credential_reads(agent, list(out.values), user=request.user, via="zk-state")
    return out


@router.post("/{slug}/vault/zk/enable", response=ZkStateOut,
             summary="Turn on zero-knowledge: wraps + every value as ciphertext, atomically")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
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
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def set_agent_zk_values(request: HttpRequest, slug: str, payload: ZkValuesIn) -> ZkValuesOut:
    """Non-clobbering upsert of `{name, value_zk}`. A `kv` other than the
    current one is 409 `zk_kv_mismatch` (the vault key was rotated)."""

    agent = _zk_admin(request, slug)
    try:
        names = zk.set_values(agent, kv=payload.kv, values=payload.values, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    # Storing a value is declaring it: the caller is the agent's owner or admin.
    services.declare_credential_names(agent, names)
    return ZkValuesOut(kv=payload.kv, names=names)


@router.post("/{slug}/vault/zk/wraps", response=ZkStateOut,
             summary="Grant: wrap the vault key to pending devices or admins")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def add_agent_zk_wraps(request: HttpRequest, slug: str, payload: ZkWrapsIn) -> ZkStateOut:
    """`{kv, wraps: [{kind: device|member, target, wrap}]}`. A device target is
    an ACTIVE key with an ECDH key on a runner this agent routes to, working for
    a member of the agent's vault; a member target is a member of the agent's
    vault with a vault key. Replaces that target's
    wrap at this kv. The recovery wrap is set only by enable and rotate."""

    agent = _zk_admin(request, slug)
    try:
        zk.add_wraps(agent, kv=payload.kv, wraps=payload.wraps, user=request.user)
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc
    return _zk_state(agent)


@router.put("/{slug}/vault/zk/pins", response=VaultKeeperPinsOut,
            summary="Record the keys a vault owner or admin just granted to, for the keepers")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def put_agent_zk_pins(request: HttpRequest, slug: str, payload: VaultKeeperPinsIn) -> VaultKeeperPinsOut:
    """`{kv, expected_version, value}`: the keepers' record, re-encrypted under the
    vault key with the people the caller just granted to. Granting someone is how an
    owner or admin confirms their key — a first one, or one they started over with —
    so keepers trust it from then on. 409 `keeper_pins_stale`: read again, merge."""

    _, vault = _agent_vault(request, slug, manage=True)
    try:
        return VaultKeeperPinsOut(**vault_keeper.store_pins(
            vault, kv=payload.kv, expected_version=payload.expected_version, value=payload.value))
    except zk.ZkError as exc:
        raise zk_problem(exc) from exc


@router.post("/{slug}/vault/zk/rotate", response=ZkStateOut,
             summary="Rotate the vault key: replace every wrap and value atomically")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def rotate_agent_zk(request: HttpRequest, slug: str, payload: ZkRotateIn) -> ZkStateOut:
    """`{new_kv: kv + 1, wraps, values}`. Every target holding a wrap now that
    still qualifies must get one at `new_kv` (422 `zk_rewrap_required`
    otherwise); a revoked device or a removed member is simply left out. A fresh
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


# ---- vault membership (spec 2026-10-22-vault-access-follows-canopy-access-design.md) ------
# Membership is the vault's own (direct members + links); writes are for its
# managers, and from a browser need a passkey step-up like every vault write.


def _vault_out(vault, user) -> VaultMembersOut:
    ms = vault_members.members(vault)
    keyed = set(UserVaultKeyset.objects.filter(user_id__in=list(ms)).values_list("user_id", flat=True))
    link = vault.links.filter(kind="agent", agent=vault.agent).first() if vault.agent_id else None
    rank = {"owner": 0, "admin": 1, "member": 2}
    rows = sorted(ms.values(), key=lambda m: (rank[m.role], (m.user.email or "").lower()))
    held, keepers = zk.copies(vault.agent)
    return VaultMembersOut(
        vault_id=str(vault.pk), kind=vault.kind, name=vault.name,
        agent_link={"linked": link is not None, "role": link.role if link else "member"},
        members=[{"user_id": m.user.pk, "email": m.user.email or "", "role": m.role, "sources": m.sources,
                  "has_vault_key": m.user.pk in keyed, "has_copy": m.user.pk in held} for m in rows],
        you=(ms[user.pk].role if user.is_authenticated and user.pk in ms else None),
        holders={"has_key": bool(vault.agent.zk_enabled and vault.agent.zk_kv),
                 "people": sorted(ms[u].user.email or "" for u in held), "keepers": keepers})


def _agent_vault(request: HttpRequest, slug: str, *, manage: bool):
    agent = agent_for_read(request, slug)
    vault = vault_members.vault_for_agent(agent)
    if vault is None:
        raise ProblemError(404, "No vault", detail="This agent is in no workspace, so it has no vault.",
                           type_=TYPE_NOT_FOUND)
    if manage:
        if not vault_members.is_admin(vault_members.role_of(vault, request.user)):
            raise HttpError(403, "this action requires someone who manages this vault")
        if stepup.is_browser(request):
            stepup.require_recent_step_up(request)
    return agent, vault


def _membership_problem(exc: vault_members.VaultMembershipError) -> ProblemError:
    return ProblemError(exc.status, "Vault membership", detail=exc.detail,
                        type_=TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION, members={"error": exc.code})


@router.get("/{slug}/vault/members", response=VaultMembersOut,
            summary="Who may open the agent's vault, and why")
@gate("member")
def get_agent_vault_members(request: HttpRequest, slug: str) -> VaultMembersOut:
    """Every member of the agent's vault with their role (`owner`, `admin` or `member`, the agent's roles) and
    every source of their membership: `direct` (added by name) or a link
    (`agent:<slug>` — full access to the agent). `you` is the caller's role."""

    _, vault = _agent_vault(request, slug, manage=False)
    return _vault_out(vault, request.user)


@router.post("/{slug}/vault/members", response=VaultMembersOut,
             summary="Add a person to the agent's vault by name")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def add_agent_vault_member(request: HttpRequest, slug: str, payload: VaultMemberIn) -> VaultMembersOut:
    """`{email, role}` — a direct membership, kept whatever happens to the
    agent's access list. The person must be a member of the agent's workspace."""

    _, vault = _agent_vault(request, slug, manage=True)
    user = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if user is None:
        raise ProblemError(422, "Vault membership", detail=f"No canopy user has the address {payload.email}.",
                           type_=TYPE_VALIDATION, members={"error": "vault_no_such_user"})
    try:
        vault_members.add_member(vault, user, role=payload.role, by=request.user)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


@router.delete("/{slug}/vault/members/{user_id}", response=VaultMembersOut,
               summary="Remove a person's direct membership of the agent's vault")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def remove_agent_vault_member(request: HttpRequest, slug: str, user_id: int) -> VaultMembersOut:
    """Removes only the DIRECT membership. Membership through a link stays until
    the person leaves the linked set (e.g. loses full access to the agent). A
    person who is no longer a member is refused the vault at once: canopy-web
    hands them, and their machines, no copy of its key and none of its values."""

    _, vault = _agent_vault(request, slug, manage=True)
    try:
        vault_members.remove_member(vault, user_id)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


@router.put("/{slug}/vault/agent-link", response=VaultMembersOut,
            summary="Link the agent's vault to the agent's access list, or unlink it")
@gate("vault-admin", setting=True, step_up=True)  # browser: + step-up; token allowed
def set_agent_vault_link(request: HttpRequest, slug: str, payload: VaultAgentLinkIn) -> VaultMembersOut:
    """`{linked, role}`. Linked, everyone with full access to the agent is a
    member: its owner as `owner`, its admins as `admin`, everyone else as `role`.
    Unlinked, only direct members remain. Refused (409 `vault_needs_an_admin`) when
    it would leave the vault with no owner or admin."""

    agent, vault = _agent_vault(request, slug, manage=True)
    try:
        vault_members.set_agent_link(vault, agent, linked=payload.linked, role=payload.role, by=request.user)
    except vault_members.VaultMembershipError as exc:
        raise _membership_problem(exc) from exc
    return _vault_out(vault, request.user)


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
             f"on {agent.slug}'s Credentials page (this call was not made from one of its chats)")
    return _secret_request_out(req, message=(
        f"Asked for {req.name}: a card to enter it is shown {where}. Its value is sealed in the "
        "person's browser and never reaches you or canopy. Do not ask for it in the conversation."))


@router.get("/{slug}/secret-requests", response=list[SecretRequestOut],
            summary="This agent's open secret requests (never a value)")
@gate("agent-admin")  # names and reasons, never a value
def list_agent_secret_requests(request: HttpRequest, slug: str) -> list[SecretRequestOut]:
    """The requests still waiting for an answer, newest first — what the agent's
    Credentials page shows its admins, including a request made outside any of
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
    if not agent.zk_enabled:
        raise zk_problem(zk_endorse.ZkError(409, "zk_not_enabled", f"{agent.slug} is not zero-knowledge"))
    return ZkRecipientsOut(
        agent=agent.slug, agent_id=str(agent.pk), kv=agent.zk_kv,
        recipients=zk_endorse.recipients(agent), endorsements=zk_endorse.endorsements(agent),
        declared=sorted(services.declared_credential_names(agent)),
        stored=sorted(AgentCredential.objects.filter(agent=agent).values_list("name", flat=True)))


@router.post("/{slug}/vault/zk/endorsements", response=ZkEndorsementsOut,
             summary="Vouch, with your own vault key, that keys may read this agent")
@gate("agent-admin", "vault-admin", setting=True, step_up=True)  # own vault key's signatures; browser: + step-up
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
@gate("member")
def list_sender_trust(request: HttpRequest, slug: str) -> list[SenderTrustOut]:
    """Addresses whose mail is tied to their member account although it is not
    DMARC- or DKIM-aligned — each with why, and when it lapses (expired rules
    are listed with `active: false` until removed)."""

    agent = agent_for_read(request, slug)
    return [_sender_trust_out(r) for r in
            SenderTrust.objects.filter(agent=agent).select_related("created_by")]


@router.put("/{slug}/sender-trust/{email}", response=SenderTrustOut,
            summary="Temporarily trust one member's unaligned email")
@gate("agent-admin", setting=True)
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
@gate("agent-admin", setting=True)
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
@gate("member")
def get_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    """Whose GitHub identity this agent's pull requests use, when that token
    expires, and whether it can open a pull request on the agent's repo."""
    agent = agent_for_read(request, slug)
    return _github_out(request, agent)


def _github_out(request: HttpRequest, agent) -> AgentGitHubOut:
    return AgentGitHubOut(**delegations.status(agent), can_lend=agent.is_admin(request.user))


@router.put("/{slug}/github", response=AgentGitHubOut,
            summary="Lend this agent your GitHub identity (owner or admin, write-only)")
@gate("agent-owner", setting=True, step_up=True)  # strictly the agent's own owner (delegations.set_github)
def set_agent_github(request: HttpRequest, slug: str, payload: AgentGitHubIn) -> AgentGitHubOut:
    """Store the caller's fine-grained GitHub token for this agent — its owner's,
    or an admin's (used while the owner has lent none).

    Checked before it is stored: GitHub must accept it, and it must be able to
    open a pull request on the agent's own repo. A token that fails is refused
    with the reason, never saved. A browser needs a fresh passkey step-up
    (403 `step_up_required` otherwise); a token may call it."""
    agent = agent_for_read(request, slug)
    if not agent.is_admin(request.user):   # before the step-up, as delegations.set_github says it
        raise HttpError(422, f"only {agent.slug}'s owner or an admin can lend it their GitHub identity")
    if stepup.is_browser(request):
        stepup.require_recent_step_up(request)
    try:
        delegations.set_github(agent, request.user, payload.token)
    except delegations.DelegationError as exc:
        raise HttpError(422, str(exc)) from exc
    return _github_out(request, agent)


@router.post("/{slug}/github/check", response=AgentGitHubOut,
             summary="Re-check this agent's GitHub token against GitHub")
@gate(
    "member",
    kind="action",
    viewer_write=(
        "re-probes the stored token against GitHub and records the result; grants and changes nothing"
    ),
)
def check_agent_github(request: HttpRequest, slug: str) -> AgentGitHubOut:
    agent = agent_for_read(request, slug)
    delegations.check_github(agent)
    return _github_out(request, agent)


@router.delete("/{slug}/github", response=AgentGitHubOut,
               summary="Withdraw your GitHub identity from this agent")
# removes only the caller's own delegation
@gate("member", "self", setting=True, viewer_write="withdraws only the caller's OWN GitHub delegation")
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
@gate("member")
def get_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.put("/{slug}/salesforce", response=AgentSalesforceOut,
            summary="Lend this agent another agent's Salesforce identity (owner of both)")
@gate("agent-owner", setting=True)  # owner of BOTH borrower and lender (delegations.set_salesforce)
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
@gate(
    "member",
    kind="action",
    viewer_write=(
        "re-probes the lender's credential against Salesforce and records the result; grants and changes nothing"
    ),
)
def check_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    delegations.check_salesforce(agent)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


@router.delete("/{slug}/salesforce", response=AgentSalesforceOut,
               summary="Stop lending this agent a Salesforce identity")
# removes only the caller's own delegation
@gate("member", "self", setting=True, viewer_write="withdraws only the caller's OWN Salesforce delegation")
def delete_agent_salesforce(request: HttpRequest, slug: str) -> AgentSalesforceOut:
    agent = agent_for_read(request, slug)
    delegations.clear_salesforce(agent, request.user)
    return AgentSalesforceOut(**delegations.salesforce_status(agent))


# Registered AFTER the literal `status`/`resolve` paths on purpose: Django
# resolves in order, so a `{name}` pattern declared first swallows "status" and
# answers 405 Method Not Allowed — the route exists, it is simply unreachable.
@router.delete("/{slug}/credentials/{name}", response=list[AgentCredentialStatusOut],
               summary="Remove one named secret")
@gate("agent-admin", setting=True)
def delete_agent_credential(request: HttpRequest, slug: str, name: str):
    agent = agent_for_admin(request, slug)
    services.delete_agent_credential(agent, name)
    return services.agent_credential_status(agent)


@router.get("/{slug}/credentials/{name}/reads", response=list[AgentCredentialReadOut],
            summary="Who read this credential's value, from which box and turn")
@gate("agent-admin")  # the read log; never a value
def agent_credential_reads(request: HttpRequest, slug: str, name: str, limit: int = 100):
    """The read log (canopy-web#1538), newest first. Never the value. The
    agent's owner or an admin — the same people who may set the value."""
    agent = agent_for_admin(request, slug)
    return services.credential_reads(agent, name, limit=max(1, min(limit, 500)))


@router.get("/{slug}/device-keys", response=list[RunnerDeviceKeyOut],
            summary="Runner device keys that could prove themselves for this agent")
@gate("agent-admin")  # can_manage per key = runners.may_trust_device_keys
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
@gate("agent-admin")  # read-anomaly alerts; never a value
def agent_credential_alerts(request: HttpRequest, slug: str, limit: int = 50):
    """Canopy Vault (canopy-web#1549), newest first: reads with no turn id by
    someone not the owner or an admin, reads of undeclared refs, bursts, and
    resolves refused for a bad or missing device proof. At most one per kind
    per hour (`occurrences` counts the rest). The agent's owner or an admin."""

    agent = agent_for_admin(request, slug)
    return vault_alerts.alerts_for(agent, limit=max(1, min(limit, 200)))


@router.get("/{slug}/pat-rotation", response=PatRotationOut,
            summary="This agent's canopy-pat rotation: setting, next due, history")
@gate("agent-admin")
def get_pat_rotation(request: HttpRequest, slug: str) -> PatRotationOut:
    """Never a token. The agent's owner or an admin."""

    agent = agent_for_admin(request, slug)
    return PatRotationOut(**pat_rotation.status(agent))


@router.patch("/{slug}/pat-rotation", response=PatRotationOut,
              summary="Turn automatic canopy-pat rotation on or off")
@gate("agent-admin", setting=True)  # turning it on also asks mint_agent_canopy_token's guard
def set_pat_rotation(request: HttpRequest, slug: str, payload: PatRotationIn) -> PatRotationOut:
    """Canopy Vault (canopy-web#1549). When on, every `period_days` (default
    30) canopy-web mints a new token for the agent's own canopy login, writes
    it to the agent's `canopy-pat` credential (a runner picks it up at its next
    `canopy cred sync`), keeps the old token valid for 24 h and then revokes it.
    The agent's token expires (the PAT default, 180 days), so `period_days` is
    capped 14 days short of that and the token is always replaced first.
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
    agent.pat_rotation_days = min(payload.period_days, pat_rotation.max_days())
    agent.save(update_fields=["pat_rotation_enabled", "pat_rotation_days",
                              "pat_rotation_enabled_by", "updated_at"])
    audit.note_change(scope=f"agent:{agent.slug}/pat-rotation",
                      before=f"{'on' if before[0] else 'off'}/{before[1]}d",
                      after=f"{'on' if payload.enabled else 'off'}/{payload.period_days}d")
    return PatRotationOut(**pat_rotation.status(agent))


@router.post("/{slug}/pat-rotation/rotate", response=PatRotationOut,
             summary="Rotate this agent's canopy-pat now")
@gate("agent-admin", setting=True, step_up=True)  # browser: + passkey step-up; token allowed
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


@router.post("/{slug}/pat-rotation/own", response=OwnPatRotateOut,
             summary="An agent rotates its OWN canopy token because it may have leaked")
# the agent's own login only: rotating its own credential can only shorten the old
# one's life, so it needs no admin — and an agent that leaked its token must be able
# to act at once, not wait for a person (Jonathan, 2026-10-24)
@gate("agent-login", setting=True, step_up="a token call by the agent's own login; carries no secret")
def rotate_own_pat(request: HttpRequest, slug: str, payload: OwnPatRotateIn) -> OwnPatRotateOut:
    """Call this the moment you think your canopy token (CANOPY_WEB_PAT) left
    where it belongs — sent to a third-party host, pasted into a chat or a log,
    committed. Same rotation as the schedule: a new token goes into your stored
    CANOPY_WEB_PAT (each box picks it up at its next `canopy cred sync`), the old
    one keeps working for 24 h so your other boxes don't break, then is revoked.
    `reason` is recorded with the rotation and in the settings audit, where your
    owner sees it. Only the agent's own login; anyone else gets a 403."""

    agent = agent_for_read(request, slug)
    if not agent.is_own_login(request.user):
        raise HttpError(403, "only the agent's own login may rotate its own token; an admin uses /pat-rotation/rotate")
    try:
        kind = token_kinds.of_request(request)
        presented = (getattr(request, "auth_credential", None) or {}).get("id") \
            if kind is not None and kind.name == token_kinds.PAT else None
        row = pat_rotation.rotate(agent, trigger="self", actor=request.user, presented_token_id=presented)
    except pat_rotation.RotationRefused as exc:
        raise HttpError(exc.status, str(exc)) from exc
    row.detail = f"{row.detail}. Self-requested: {payload.reason}"[:500]
    row.save(update_fields=["detail"])
    audit.note_change(scope=f"agent:{agent.slug}/canopy-pat", before="current token",
                      after=f"rotated by the agent itself: {payload.reason}"[:300])
    return OwnPatRotateOut(rotated_at=row.created_at, old_token_revoked_after=row.revoke_after, detail=row.detail)


@router.post("/{slug}/credentials/{name}/reveal", response=AgentCredentialRevealOut,
             summary="PLAINTEXT — show one stored value to an admin who just confirmed a passkey")
# browser session + passkey step-up only (apps/passkeys)
# reads one value; changes no configuration (recorded as an event)
@gate("agent-admin", kind="action", step_up=True)
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
