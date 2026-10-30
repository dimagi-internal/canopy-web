"""/api/agents/{slug}/credentials… — an agent's secrets: set, status, who may read, resolve, delete, the
read log, device keys and credential alerts. The rest of the vault lives beside it: `api_vault_zk`,
`api_vault_members`, `api_secret_requests`, `api_sender_trust`, `api_github`, `api_pat_rotation`."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import (
    TYPE_AUTH,
    TYPE_CONFLICT,
    ProblemError,
)
from apps.api.route_gates import gate
from apps.harness import device_keys
from apps.harness.api_device_keys import agents_key_opens, key_out, zk_agents_waiting_on
from apps.harness.runners import may_trust_device_keys
from apps.harness.schemas import RunnerDeviceKeyOut
from apps.passkeys import stepup

from . import services
from .credentials import (
    accounts,
    credential_resolve,
    vault_alerts,
)
from .credentials.zk import ZkError
from .gates import (
    agent_for_admin,
    agent_for_read,
)
from .route_refusals import zk_problem
from .schemas import (
    AgentCredentialAccessOut,
    AgentCredentialAlertOut,
    AgentCredentialReadOut,
    AgentCredentialsIn,
    AgentCredentialsResolveOut,
    AgentCredentialStatusOut,
    DeviceProofRequiredOut,
    ZkWrapMissingOut,
)
from .vault_gates import vault_manager

router = Router(auth=session_auth, tags=["agents"])

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
    agent = vault_manager(request, slug)
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
    than the caller.

    `delegated_withheld` names the credentials people delegated to the agent: a box that
    runs it gets those, nobody else, so on your own machine you use your own account
    for each (`canopy agent use` says so)."""
    agent = agent_for_read(request, slug)
    via, reason = credential_resolve.credential_access(request, agent)
    return AgentCredentialAccessOut(
        agent=agent.slug,
        may_resolve=via is not None, via=via, reason=reason,
        delegated_withheld=accounts.withheld_payload(agent),
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
    agent also declares (`runtime_secrets`). `mailbox` is not a secret and is always
    returned.

    **Delegated credentials** (a token a person gave the agent from their own
    account, such as its GitHub token) never ride `values`. They come only in
    `delegated`, sealed (`zks1:`) to the machines that run the agent, and only to a
    box that runs it reading with its own device proof — never to an admin or a
    license holder.

    **Device proof.** With zero-knowledge on (every agent that holds a value), the request must also carry a `DPoP` header: an RFC 9449
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

    **Alerts.** The agent's owner is told
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
       - `license`: holds an ACTIVE license to use the agent directly and names
         their OWN open direct turn on it in `X-Canopy-Turn-Id` (canopy-web#1704).
         The read is logged against that turn and license.
       Anyone else — a member, an editor with no direct turn — is refused, and
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


# Registered AFTER the literal `status`/`resolve` paths on purpose: Django
# resolves in order, so a `{name}` pattern declared first swallows "status" and
# answers 405 Method Not Allowed — the route exists, it is simply unreachable.
@router.delete("/{slug}/credentials/{name}", response=list[AgentCredentialStatusOut],
               summary="Remove one named secret")
@gate("agent-admin", setting=True)
def delete_agent_credential(request: HttpRequest, slug: str, name: str):
    agent = vault_manager(request, slug)
    # A credential a person delegated is theirs to withdraw, never an admin's to delete.
    try:
        accounts.refuse_delegated_names(agent, [name])
    except ZkError as exc:
        raise zk_problem(exc) from exc
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
    the keys that may read its secrets once active. `can_manage`
    says whether the caller may approve each (the runner's owner or an admin of
    it; on a private runner, its owner alone)."""

    agent = agent_for_admin(request, slug)
    vault = [agent]
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


