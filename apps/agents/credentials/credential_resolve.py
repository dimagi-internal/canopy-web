"""Who may read an agent's PLAINTEXT secrets, and what they are handed.

The decisions behind `GET /api/agents/{slug}/credentials/resolve` (and the
cheap `/credentials/access` check, which asks the same `credential_access`):
the bearer-only rule, which principal the caller is (`runner` / `admin` /
`license`), device-proof matching, the zero-knowledge wrap requirement, what a
license read never gets, how the read is classified for the log, and the read
log, alerts and fleet event that follow every read.

`resolve` returns a `Resolved` (the response's fields) or a `Refusal` (what
was refused and why). It raises nothing for a refusal; the route only
translates one to its HTTP answer. The request goes in because the proofs are
over it (a DPoP proof binds the method and URL) and the alerts quote it.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest

from apps.events import services as events
from apps.harness import aws_iam_proof, device_keys
from apps.harness.models import Runner

from .. import mailbox, services
from . import accounts, authority, licenses, vault_alerts, vault_members, workspace_vaults, zk
from .pat_rotation import CANOPY_PAT_REF

logger = logging.getLogger(__name__)

#: Which of the agent-only kind (`authority`) live in the vault's `values`, by name:
#: `CANOPY_PAT_REF` is the name the agent's login is actually stored under (minted,
#: rotated); the two kebab names are older spellings some vaults still carry.
AGENT_ONLY_REFS = frozenset({CANOPY_PAT_REF, "canopy-pat", "canopy-web-pat"})

BEARER_REQUIRED = "bearer_required"
NO_ACCESS = "no_access"
FOREIGN_DEVICE_PROOF = "foreign_device_proof"
DEVICE_PROOF_REQUIRED = "device_proof_required"
ZK_WRAP_MISSING = "zk_wrap_missing"
NOT_A_VAULT_MEMBER = "not_a_vault_member"


@dataclass(frozen=True)
class Refusal:
    """Why a resolve hands out nothing. `code` is one of the constants above;
    `detail` is what the caller is told. `reason` (device proof: `not_routed` /
    `no_valid_proof`), `kv` and `device_key` (zk wrap) only where they apply."""

    code: str
    detail: str
    reason: str = ""
    kv: int | None = None
    device_key: str = ""


@dataclass
class Resolved:
    """The fields of `AgentCredentialsResolveOut`, and `via`: who read them."""

    values: dict[str, str]
    mailbox: str
    mailbox_backend: str
    google_account: Any
    zk: bool
    kv: int | None
    wrap: Any
    agent_id: str
    vaults: list = field(default_factory=list)
    #: Credentials people delegated to the agent, sealed to this machine: {name:
    #: {value_zk, provider, identity, delegated_by_email, waiting}} (`accounts`).
    delegated: dict = field(default_factory=dict)
    delegated_withheld: list = field(default_factory=list)
    via: str = field(default="", compare=False)

    def response_fields(self) -> dict:
        return {k: v for k, v in vars(self).items() if k != "via"}


def mailbox_backend(agent) -> str:
    """"ses" when the agent's address is canopy-hosted (nothing to sign in; `canopy email`
    goes through canopy-web), "google" for any other address, "" with none."""
    kind = mailbox.describe(agent)["kind"]
    return {"hosted": "ses", "google": "google"}.get(kind, "")


def credential_access(request: HttpRequest, agent) -> tuple[str | None, str]:
    """Who the caller is, for the purpose of reading this agent's plaintext:
    `("runner", "")`, `("admin", "")`, `("license", "")`, or `(None, <what access to get>)`.

    The one decision both `/credentials/resolve` and `/credentials/access`
    make, so the cheap check can never disagree with the real gate. A request
    naming the person's own open direct turn is `license` whoever they are, an
    admin or a runner owner included (`authority`: a direct session). Otherwise
    `runner` wins over `admin` when both hold (`caller_runs_agent` already implies
    admin), because it is the more specific claim and the audit trail should say a
    box read it.

    A runner token (`RunnerAccessToken`, bought with a device key) signs in as
    the runner's owner, but it speaks for ONE box: it is a `runner` caller only
    for an agent that box serves (`device_keys.runner_serves_agent`), and never
    an `admin` — the owner's other agents stay out of a box's reach.
    """
    rtok = getattr(request, "runner_token", None)
    if rtok is not None:
        # A person's computer (no runner app) is never a `runner` reader: its token
        # reads only under its owner's license, during their own direct turn.
        if (rtok.runner.kind != Runner.COMPUTER and agent.is_admin(request.user)
                and device_keys.runner_serves_agent(rtok.runner, agent)):
            return "runner", ""
        # A PRIVATE runner reads under its owner's license, for one of their own turns
        # it holds (canopy-web#1705) — the same principal as their direct sessions.
        if license_for_read(request, agent) is not None:
            return "license", ""
        return None, (f"this runner token is {rtok.runner.name}'s, and {rtok.runner.name} "
                      f"does not run {agent.slug}")
    # A person's OWN open direct turn on this agent (named in X-Canopy-Turn-Id) is a
    # direct session, whoever they are: an owner or admin using the agent in their own
    # Claude Code is read like anyone else, and steps up like anyone else
    # (`authority`). Asked first so being a runner owner or an admin changes nothing.
    # apps/agents/credentials/licenses.py, canopy-web#1704. No turn, no secrets.
    if license_for_read(request, agent) is not None:
        return "license", ""
    if services.caller_runs_agent(request.user, agent):
        return "runner", ""
    if agent.is_admin(request.user):
        return "admin", ""
    owner = getattr(agent.owner, "email", "") if agent.owner_id else ""
    ask = f"ask its owner ({owner})" if owner else "ask its owner or a workspace owner"
    return None, (
        f"resolving {agent.slug}'s secrets needs a live runner you pair that "
        f"{agent.slug} routes to, being {agent.slug}'s owner or an agent admin, or an active "
        f"license to use it directly, during your own direct session (canopy private-runner register, "
        f"then open a session with CANOPY_AGENT={agent.slug}); "
        f"{ask} to make you an admin (PUT /api/agents/{agent.slug}/admins/<your user id>)"
    )


def license_for_read(request: HttpRequest, agent):
    """The AgentLicense a credential read may go out under (`licenses.credential_principal`)."""
    rtok = getattr(request, "runner_token", None)
    if rtok is not None and not rtok.runner.private:
        return None  # a fleet box speaks for itself, never for a person's license
    return licenses.credential_principal(
        agent, request.user, request.META.get("HTTP_X_CANOPY_TURN_ID", ""),
        runner=rtok.runner if rtok is not None else None)


def parse_refs(refs: str | None) -> set[str] | None:
    """`?refs=a,b` → {"a", "b"}; absent → None (unscoped). Present but empty
    (`?refs=`) is a scope of nothing, never "everything"."""
    if refs is None:
        return None
    return {r.strip() for r in refs.split(",") if r.strip()}


def device_key_from_proof(request: HttpRequest, agent):
    """`(key, refused, unrouted)`: the ACTIVE RunnerDeviceKey whose proof this
    request carries, if the proof verifies and its runner serves `agent`; else
    None and why. `unrouted` is the runner when the proof is GOOD — an approved,
    active key — and only the routing says no: the box simply does not run this
    agent (`device_keys.runner_serves_agent`). That is a different answer from a
    proof nobody approved, and the two get different follow-ups. A bad proof is
    not an error here — whether one is REQUIRED is the caller's decision.

    Two proofs: a DPoP proof from a key the box holds (`DPoP`, Phase 1a), or an
    EC2 box's instance role (`X-Canopy-Aws-Proof`, Phase 1b — a signed
    sts:GetCallerIdentity canopy-web replays to the pinned STS endpoint;
    `apps/harness/aws_iam_proof.py`)."""
    aws = request.META.get(aws_iam_proof.PROOF_HEADER, "")
    if aws:
        try:
            role = aws_iam_proof.verify_caller(aws, request)
        except aws_iam_proof.AwsProofError as exc:
            logger.info("aws iam proof refused for %s: %s", agent.slug, exc)
            return None, f"AWS IAM proof refused: {exc}", None
        key = aws_iam_proof.active_key_for_agent(agent, role)
        if key is not None:
            return key, None, None
        known = aws_iam_proof.known_key(role)
        if known is not None:
            return None, f"{known.runner.name} does not run {agent.slug}", known.runner
        return None, f"no approved device key for {aws_iam_proof.canonical_role_arn(role)}", None
    if not request.META.get("HTTP_DPOP"):
        return None, "no device proof", None
    try:
        jkt = device_keys.verify_proof(request)
    except device_keys.ProofError as exc:
        logger.info("device proof refused for %s: %s", agent.slug, exc)
        return None, f"DPoP proof refused: {exc}", None
    key = device_keys.active_key_for_agent(agent, jkt)
    if key is not None:
        return key, None, None
    known = device_keys.known_key(jkt)
    if known is not None:
        return None, f"{known.runner.name} does not run {agent.slug}", known.runner
    return None, "the DPoP key is not an approved device key", None


def caller_runner(request: HttpRequest):
    """The runner named by `X-Canopy-Runner`, only when the caller owns it —
    a header naming someone else's box is ignored rather than recorded."""
    raw = request.META.get("HTTP_X_CANOPY_RUNNER", "").strip()
    if not raw:
        return None
    try:
        rid = uuid.UUID(raw)
    except ValueError:
        return None
    return Runner.objects.filter(pk=rid, owner=request.user).first()


def resolve(request: HttpRequest, agent, refs: str | None) -> Resolved | Refusal:
    """Everything `/credentials/resolve` decides, for an agent the caller may
    already reach (`gates.agent_for_read`). See the route for the contract."""
    # Bearer only: a session cookie is refused even for the owner, so "the
    # browser never sees plaintext" is a property of the system, not of the UI.
    # A bearer that AUTHENTICATED the request (the middleware stamps
    # `auth_method`), not merely a header: a cookie plus `Authorization: Bearer
    # junk` is still a browser.
    if not getattr(request, "auth_method", None):
        return Refusal(BEARER_REQUIRED,
                       "resolve requires a bearer token; a browser session is never given values")

    via, reason = credential_access(request, agent)
    if via is None:
        return Refusal(NO_ACCESS, reason)

    # Device proof (canopy-web#1538 Phase 1a). Verified whenever one is sent, so
    # the read log can name the box that proved itself; REQUIRED for a zero-knowledge
    # agent (below), which is every agent that holds a value.
    device_key, refused, unrouted = device_key_from_proof(request, agent)
    # A runner token speaks for one box: a proof from some other box's key does
    # not go with it.
    rtok = getattr(request, "runner_token", None)
    if rtok is not None and device_key is not None and device_key.runner_id != rtok.runner_id:
        return Refusal(FOREIGN_DEVICE_PROOF, f"the device proof is {device_key.runner.name}'s, not this "
                                             f"runner token's ({rtok.runner.name})")
    # A zero-knowledge agent ALWAYS needs one (canopy-web#1550): the wrap handed
    # out is the proving device's, so there is nothing to give without it. Any
    # ciphertext it does hold is useless without a device's own key, and a `zk1`
    # value with no wrap is still the refusal below.
    if device_key is None and agent.zk_enabled:
        if unrouted is not None:
            # An APPROVED box that does not run this agent: a routing fact, not a
            # misused token, so the owner is not emailed (fizzy, canopy-web#1563).
            vault_alerts.unrouted_box_refused(agent, request, runner=unrouted)
            return Refusal(DEVICE_PROOF_REQUIRED, refused, reason="not_routed")
        # Name the box when the request does (its runner token, or an owned
        # X-Canopy-Runner), so the owner is not left asking which machine.
        box = rtok.runner if rtok is not None else caller_runner(request)
        vault_alerts.device_proof_refused(agent, request, reason=refused or "no device proof",
                                          runner=box)
        return Refusal(DEVICE_PROOF_REQUIRED, refused or "no device proof", reason="no_valid_proof")

    wanted = parse_refs(refs)
    zk_wrap = None
    if agent.zk_enabled and device_key is not None and not vault_members.device_works_for_a_member(agent, device_key):
        # The machine layer never reads on its own: the person this box works for
        # must be a member of the agent's vault (spec 2026-10-22-vault-access-
        # follows-canopy-access-design.md), whatever copies it still holds.
        return Refusal(NOT_A_VAULT_MEMBER,
                       f"{device_key.runner.name} works for someone who is not a member of "
                       f"{agent.slug}'s vault; a manager of the vault must add them")
    if agent.zk_enabled:
        values, zk_wrap = zk.resolve_payload(agent, device_key, wanted)
        # `zks1` values (server-minted, sealed to each device) open with the
        # device's own key; only `zk1` values need its vault-key wrap.
        if zk_wrap is None and zk.needs_wrap(values):
            if device_key is not None:
                zk.device_wrap_missing(agent, request, device_key)
            return Refusal(ZK_WRAP_MISSING,
                           f"this device holds no vault-key wrap at kv {agent.zk_kv}; "
                           "an admin must grant it",
                           kv=agent.zk_kv, device_key=str(device_key.pk) if device_key else "")
    else:
        # An agent that has never stored a value here: there is nothing to hand out
        # (a first stored value turns its vault on).
        values = {}
    placement = authority.placement_of(via)

    # A box that runs the agent, by its own proof: never an admin reading by hand (the
    # owner at their own laptop is an `admin` read, `_classify_read`).
    runner_read = (via == "runner" and device_key is not None
                   and _classify_read(request, via, runner=device_key.runner, device_key=device_key,
                                      rtok=rtok) == "runner")
    delegated = (accounts.resolve_payload(agent, device_key)
                 if runner_read and authority.hands_out(authority.RUNNER, authority.DELEGATED) else {})
    # Anyone else (an admin or license holder using the agent on their own machine) is
    # told which credentials are someone's own and not theirs to use.
    withheld = [] if runner_read else accounts.withheld_payload(agent)

    # A person in a direct session runs the agent's TOOLS, not the agent: never its own
    # canopy login (which grades as `system` — past the editor's manual cap) — canopy-web#1704.
    hold_back_login = not authority.hands_out(placement, authority.AGENT_ONLY)
    if hold_back_login:
        values = {k: v for k, v in values.items() if k not in AGENT_ONLY_REFS}
    # The workspace vaults layered under the agent's own (only a device can open them).
    layers = workspace_vaults.layers_payload(agent, device_key, wanted) if device_key is not None else []
    if hold_back_login:
        for layer in layers:
            layer["values"] = {k: v for k, v in layer["values"].items() if k not in AGENT_ONLY_REFS}
    out = Resolved(
        values=values,
        mailbox=agent.email or "",
        mailbox_backend=mailbox_backend(agent),
        google_account=agent.google_identity(),
        delegated=delegated,
        delegated_withheld=withheld,
        zk=agent.zk_enabled, kv=agent.zk_kv if agent.zk_enabled else None, wrap=zk_wrap,
        agent_id=str(agent.pk) if agent.zk_enabled else "",
        vaults=layers,
    )
    read = (list(values) + list(delegated)
            + [f"{layer['name']}/{n}" for layer in layers for n in layer["values"]])
    runner = device_key.runner if device_key is not None else caller_runner(request)
    out.via = via = _classify_read(request, via, runner=runner, device_key=device_key, rtok=rtok)
    _record_read(request, agent, via=via, runner=runner, wanted=wanted, values=values, read=read)
    return out


def _classify_read(request: HttpRequest, via: str, *, runner, device_key, rtok) -> str:
    """Whose read this was, for the log and the alerts: a `runner` caller is only a
    BOX reading when the request names a box that is not its owner's own laptop."""
    # A read is a BOX's only when this request names one: a device proof, or an
    # `X-Canopy-Runner` the caller owns. Pairing a runner is a fact about the
    # caller, not the request, so an owner who also pairs a box and resolves by
    # hand from their own session is an admin read, not a box outside a turn
    # (canopy-web#1563). Every `runner` caller is an admin already.
    if via == "runner" and runner is None:
        return "admin"
    # The owner at their OWN laptop: their own token (not the box's runner token),
    # proved by that laptop's approved device key. A zero-knowledge agent always
    # needs the proof, so without this every emdash session the owner opened read
    # as "a box outside a turn" and emailed them about themselves (2026-10-18).
    # The read log still names the laptop. A cloud box, a bare X-Canopy-Runner, a
    # runner token, or someone else's laptop is still the box reading.
    if (via == "runner" and rtok is None and device_key is not None
            and runner.kind == runner.EMDASH and runner.owner_id == request.user.pk):
        return "admin"
    return via


def _record_read(request: HttpRequest, agent, *, via: str, runner, wanted, values, read) -> None:
    """The read log, the owner's alerts and the fleet event. None of them may
    deny a runner its secrets, so none of them raises."""
    turn_id = request.META.get("HTTP_X_CANOPY_TURN_ID", "")[:120]
    lic = None
    if via == "license":
        lic = license_for_read(request, agent)
        if lic is not None:
            licenses.touch(lic)
    try:
        services.record_credential_reads(
            agent, read, user=request.user, runner=runner, via=via, turn_id=turn_id,
            license=lic)
    except Exception:  # noqa: BLE001 - a log hiccup must not deny a runner its secrets
        logger.exception("credential read log failed for %s", agent.slug)
    # Never raises (canopy-web#1549): an alert hiccup must not deny a runner either.
    vault_alerts.after_read(agent, request, via=via, turn_id=turn_id, asked=wanted,
                            handed=list(values), runner=runner)
    try:
        events.record(
            [{
                "source": "agents.credentials",
                "kind": "agent.credentials.resolved",
                "level": "info",
                "key": f"{agent.slug}:{request.user.pk}",
                "summary": f"{len(values)} secret(s) resolved for {agent.slug}",
                "payload": {"agent": agent.slug, "count": len(values), "via": via,
                            "refs": sorted(read), "scoped": wanted is not None,
                            "runner": str(runner.pk) if runner else "",
                            "turn_id": turn_id},
            }],
            workspace=agent.workspace,
        )
    except Exception:  # noqa: BLE001 - an audit hiccup must not deny a runner its secrets
        pass
