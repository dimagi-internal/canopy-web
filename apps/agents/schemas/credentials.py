from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from apps.common.schemas import StrictModel


# ---- shared ----
class GoogleMintStartIn(StrictModel):
    #: The Google account to preselect on the consent screen ("" = the agent's own).
    login_hint: str = ""


class GoogleMintOut(StrictModel):
    """One "Connect Google mailbox" sign-in, as the person waiting on it sees it.

    `status`: `requested` (the runner is preparing the link), `awaiting` (open
    `authorize_url` with a top-level navigation), `completing` (Google answered; the
    runner is finishing), `done` or `failed`. `outcome` is how it ended (ok, denied,
    wrong-account, …). Never a code or a token: the token is exchanged and encrypted
    on the runner, and canopy never holds it."""

    id: uuid.UUID
    status: Literal["requested", "awaiting", "completing", "done", "failed"]
    outcome: str = ""
    authorize_url: str = ""
    #: The runner finishing the sign-in.
    runner: str = ""
    #: The Google account that consented, once known.
    account: str = ""
    #: The account the agent must sign in as ("" = any).
    expected_account: str = ""
    #: A person connecting their OWN account (a delegated credential), not the agent's.
    delegated: bool = False
    #: Lent for a session of this many minutes (0 = standing).
    session_minutes: int = 0
    detail: str = ""
    created_at: datetime
    updated_at: datetime


class GoogleDelegateIn(StrictModel):
    """Connect YOUR OWN Google account to an agent, as a credential you delegate to it."""

    #: The Google account you will sign in as (you@gmail.com); the consent must match it.
    account: str = Field(min_length=3, max_length=200)
    #: gog's service names to grant: gmail (default), calendar, drive, docs, sheets.
    services: list[str] = Field(default_factory=lambda: ["gmail"], max_length=8)
    #: Lend it for this many minutes only (1-60): no refresh token is issued, and it is
    #: gone at the end. 0 = a standing delegation, until you withdraw it.
    session_minutes: int = Field(default=0, ge=0, le=60)


class SenderTrustIn(StrictModel):
    """A TEMPORARY trust rule for one member's unaligned mail (`SenderTrust`).

    `reason` is required: say why, and what fixes it for good (usually "their
    domain's admin turns on DKIM signing"). `days` is how long it lasts, 1–90;
    re-PUT to extend. There is no permanent form."""

    reason: str = Field(min_length=3, max_length=2000)
    days: int = Field(default=14, ge=1, le=90)


class SenderTrustOut(StrictModel):
    email: str
    reason: str
    expires_at: datetime
    active: bool
    created_by: str | None = None
    created_at: datetime


class CountOut(StrictModel):
    created: int = 0
    replaced: int = 0
    count: int = 0


class AgentCredentialsIn(StrictModel):
    """Upsert named secrets. NON-CLOBBERING — a ref absent from `values` is left
    alone, so a single-field edit cannot wipe the rest. There is deliberately no
    read counterpart: the only route that returns values is the runner's."""

    values: dict[str, str] = Field(default_factory=dict)


class AgentCredentialStatusOut(StrictModel):
    """Masked view — booleans and timestamps, NEVER values.

    `declared` distinguishes a ref the agent declares from an
    orphan left behind when one was removed; `source` is `canopy-web-zk` for a
    stored value (zero-knowledge ciphertext, the only kind) and `unset` otherwise."""

    name: str
    declared: bool
    set: bool
    source: str
    updated_at: dt.datetime | None = None
    updated_by_email: str | None = None
    # The read log (canopy-web#1538): when a value was last handed out, and how
    # often. History: GET …/credentials/{name}/reads.
    last_read_at: dt.datetime | None = None
    read_count: int = 0
    #: Whose account it is: "agent" (the agent's own) or "delegated" (a person's, which
    #: only the machines that run the agent can open), and the sentence that says so.
    account: str = "agent"
    account_label: str = ""


class AgentCredentialReadOut(StrictModel):
    """One read of a credential's value — who, which box, which turn, when."""

    name: str
    user_email: str = ""
    runner: str = ""
    runner_name: str = ""
    via: str = ""
    turn_id: str = ""
    created_at: dt.datetime


class DeviceProofRequiredOut(StrictModel):
    """401 from resolve (problem+json): the agent requires a runner device
    proof (DPoP) and the request carried none that verified against an active
    key. `error` and `reason` are extension members a runner branches on."""

    type: str
    title: str
    status: int
    detail: str = ""
    instance: str | None = None
    error: Literal["device_proof_required"]
    #: `not_routed`: the proof is an approved box's, but that box does not run
    #: this agent (no alert; fix the routing or stop asking). `no_valid_proof`:
    #: no proof, or one no active key matches (the owner is alerted).
    reason: Literal["not_routed", "no_valid_proof"] = "no_valid_proof"


class AgentCredentialAlertOut(StrictModel):
    """A read of the agent's secrets that looked wrong (canopy-web#1549). At
    most one per kind per hour; `occurrences` counts repeats inside it."""

    id: int
    kind: Literal["no-turn", "no-turn-check", "undeclared-ref", "burst", "device-proof", "zk-wrap-missing"]
    detail: str = ""
    refs: list[str] = []
    user_email: str = ""
    runner: str = ""
    runner_name: str = ""
    turn_id: str = ""
    occurrences: int = 1
    #: Who asked: client, User-Agent, session headers, the caller's reason for sending
    #: no device proof, and what `turn_id` names (`turn`: agent, origin, status,
    #: runner, chat session, who started it). Never a value.
    context: dict = {}
    #: The same, with `at` and `detail`, for each repeat inside the hour (capped).
    repeats: list[dict] = []
    #: The owner was emailed or pushed (false: delivery is off, or it failed, or
    #: `told_with` says another agent's alert already told them).
    notified: bool = False
    #: Not emailed because the same fault on the same runner already emailed the
    #: owner within the hour about this agent (its slug). One box fault, one email.
    told_with: str = ""
    created_at: dt.datetime
    last_seen_at: dt.datetime


class PatRotationIn(StrictModel):
    """Opt the agent's own `canopy-pat` in to (or out of) automatic rotation."""

    enabled: bool
    period_days: int = Field(default=30, ge=1, le=365)


class PatRotationEventOut(StrictModel):
    status: Literal["rotated", "failed"]
    trigger: str
    detail: str = ""
    created_at: dt.datetime
    #: When the replaced token stops working (null: nothing was replaced).
    revoke_after: dt.datetime | None = None
    old_revoked_at: dt.datetime | None = None


class PatRotationOut(StrictModel):
    """The agent's canopy-pat rotation: the setting, when it is next due, and
    its recent rotations. Never a token."""

    enabled: bool
    period_days: int
    last_rotated_at: dt.datetime | None = None
    next_due_at: dt.datetime | None = None
    enabled_by_email: str = ""
    history: list[PatRotationEventOut] = []


class OwnPatRotateIn(StrictModel):
    reason: str = Field(min_length=10, max_length=400,
                        description="What happened: where the token went, or why you think it is exposed.")


class OwnPatRotateOut(StrictModel):
    """The rotation the agent just triggered on its own login. Never a token."""

    rotated_at: dt.datetime
    old_token_revoked_after: dt.datetime | None = None
    detail: str


class AgentCredentialAccessOut(StrictModel):
    """Whether the CALLER may resolve this agent's secrets — never the values.

    The cheap check `canopy cred` makes before deciding how to resolve
    (canopy#850). `may_resolve` mirrors GET /credentials/resolve exactly, minus
    its bearer requirement (that is a property of the request, not of the
    caller): `via` is `runner` when the caller pairs a live runner this agent
    routes to, `admin` when they are the agent's owner or an admin, `license`
    when they hold an active license to use it directly and name their own open
    direct turn in `X-Canopy-Turn-Id`, and null when none holds — then `reason`
    names the access to ask for."""

    agent: str
    #: Always "canopy-web": the one store there is. Kept only because a cloud runner
    #: whose code predates 80986da7 reads it to decide whether an agent has an env to
    #: resolve, and without it resolves NONE (every turn on that box then starts with
    #: an empty env). Remove once no runner reports an older `code_sha`.
    credential_source: Literal["canopy-web"] = "canopy-web"
    may_resolve: bool
    via: Literal["runner", "admin", "license"] | None = None
    reason: str = ""
    #: The credentials people delegated to this agent (their own account, such as a
    #: GitHub token): only the machines that run it get them, so someone using the agent
    #: on their own machine uses their own account for each. Never a value.
    delegated_withheld: list[DelegatedWithheldOut] = Field(default_factory=list)


class AgentSharedVaultOut(StrictModel):
    """A workspace vault this agent's turns read, layered under its own: the agent's page
    lists these so its secrets are never only half the story. Names only, never a value."""

    vault_id: str
    name: str
    workspace: str
    #: Names only.
    secrets: list[str] = Field(default_factory=list)
    #: Why the agent gets it: "agent" (given to it by name), "project" (it is a member of a
    #: project the vault is given to) or "workspace" (its workspace, or one above, has it).
    via: str
    #: The project's address or the workspace's slug for `via` project/workspace; "" for agent,
    #: and "" for a project the caller may not read (it is not named to them).
    via_name: str = ""


class ResolveVaultLayerOut(StrictModel):
    vault_id: str
    name: str
    workspace: str
    binding_id: str
    kv: int
    wrap: dict[str, Any] | None = None
    values: dict[str, str] = Field(default_factory=dict)


class MyDelegatedCredentialOut(StrictModel):
    """One credential from your own account, delegated to an agent. Never the value."""

    id: int
    agent: str
    agent_name: str = ""
    name: str
    account: str = "delegated"
    #: "GitHub · delegated by Jonathan Jackson · GitHub says it expires 12 Jan 2027".
    label: str
    provider: str = ""
    #: The external account it acts as (`{login, user_id, name}` for GitHub).
    identity: dict[str, Any] = Field(default_factory=dict)
    delegated_by_email: str = ""
    delegated_by_agent: str = ""
    purpose: str = ""
    #: What the provider said about expiry: information only, canopy sets none.
    provider_expires_at: dt.datetime | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    checked_at: dt.datetime | None = None
    #: "server" (when it was given) or the name of the machine that probed it.
    checked_by: str = ""
    #: The only machines that can open it; their owners are the agent's admins.
    machines: list[str] = Field(default_factory=list)
    #: Machines that run the agent and wait for one that holds it to seal it to them.
    waiting: list[str] = Field(default_factory=list)
    #: You are still one of the agent's admins, so turns use it.
    in_force: bool = True
    updated_at: dt.datetime | None = None


class MyConnectedAccountOut(StrictModel):
    """Which of your own accounts an agent can use — the account and nothing about the
    credential (no machines, checks or value). What a page shows as "Connected"."""

    #: Its id, to end it (`DELETE /api/me/delegated-credentials/{id}`).
    id: int
    agent: str
    provider: str = ""
    #: The external account: an email for Google, a login for GitHub.
    account: str = ""
    #: You are still one of the agent's admins, so turns use it.
    in_force: bool = True
    #: Lent for a SESSION: when it ends (it is gone after). None = standing, until withdrawn.
    session_until: dt.datetime | None = None


class DelegatedWaitingOut(StrictModel):
    """A machine that runs the agent and may hold a delegated credential, but it was not
    sealed to it yet: a machine that holds it re-seals it (`…/vault/delegated/{name}`)."""

    kind: str = "device"
    #: The machine's ECDH key thumbprint (the zks1 target).
    target: str
    public_jwk: dict[str, Any]
    runner_name: str = ""


class DelegatedWithheldOut(StrictModel):
    """A delegated credential this read does not get, named so the reader uses their own."""

    name: str
    provider: str = ""
    label: str = ""
    delegated_by_email: str = ""
    #: What to do instead, in a sentence: in a direct session the account is the reader's own.
    use: str = ""


class DelegatedEntryOut(StrictModel):
    """One delegated credential, as a runner's resolve hands it out: ciphertext only."""

    value_zk: str
    provider: str = ""
    identity: dict[str, Any] = Field(default_factory=dict)
    delegated_by_email: str = ""
    waiting: list[DelegatedWaitingOut] = Field(default_factory=list)


class AgentCredentialsResolveOut(StrictModel):
    """PLAINTEXT, for a runner. The one route that returns values.

    There is exactly ONE plaintext gate to reason about, and one audit entry per
    fetch."""

    values: dict[str, str] = Field(default_factory=dict)
    # No GitHub token here: a token a person delegated is in `delegated` below, sealed
    # to the machines that run the agent.
    # THIS instance's mailbox (`Agent.email`), which the box sets Gmail up for.
    # Not a secret; it rides here because bootstrap already asks this route per
    # agent. It is the instance's, never the repo's — every instance of an
    # agent shares its repo, and deriving the address from the repo or the slug
    # would point two instances at one inbox (canopy-web#984).
    mailbox: str = ""
    # Where that mailbox lives: "ses" = canopy-hosted (no Gmail sign-in; mail goes through
    # canopy-web), "google" = a Google mailbox the box signs in with gog, "" = none.
    mailbox_backend: str = ""
    # The Google account the box signs in to Drive/Docs/gog as (`Agent.google_identity`):
    # `google_account` when set, else `mailbox` when that is a Google mailbox, else "".
    google_account: str = ""
    # Zero-knowledge (canopy-web#1550). When `zk`, `values` hold `zk1:`
    # ciphertext under the agent's vault key at version `kv`, and `wrap` is that
    # key wrapped to the proving device's ECDH key (docs/vault/zk-v1/). A value
    # canopy-web minted itself may instead be `zks1:`, sealed to each device's
    # ECDH key directly (§6) — opened with the device key, no wrap needed; `wrap`
    # is null when every value is `zks1` (and always at kv 0, before a vault key).
    zk: bool = False
    kv: int | None = None
    wrap: dict[str, Any] | None = None
    #: The Agent pk as a string — bound into every AAD and HKDF info ("" when not zk).
    agent_id: str = ""
    #: The workspace vaults layered UNDER the agent's own values, in order (a later
    #: layer wins a name clash; `values` above win over all of them) — spec
    #: 2026-10-22-vault-access-follows-canopy-access-design.md §4. Each is `zk1:`
    #: ciphertext under that vault's key, bound to its `binding_id`, with this
    #: device's copy of the key — or `wrap: null` and no values when this device may
    #: not open it. Only with a device proof.
    vaults: list[ResolveVaultLayerOut] = Field(default_factory=list)
    #: Credentials people delegated to the agent (their own account, given for the
    #: agent to use), each sealed (`zks1:`) to the machines that run the agent and
    #: opened with this device's own key. Only to a box that runs the agent, reading
    #: with its own proof; never to an admin or a license holder.
    delegated: dict[str, DelegatedEntryOut] = Field(default_factory=dict)
    #: For every read that does NOT get them — an admin or license holder using the agent
    #: on their own machine — the delegated credentials that are someone's own and so not
    #: theirs: name, provider and whose. Never a value. Use your own account for these.
    delegated_withheld: list[DelegatedWithheldOut] = Field(default_factory=list)


class ZkWrapMissingOut(StrictModel):
    """409 from resolve (problem+json): the agent is zero-knowledge and the
    proving device holds no vault-key wrap at the current kv (its owner is
    alerted). `error`, `kv` and `device_key` are extension members."""

    type: str
    title: str
    status: int
    detail: str = ""
    instance: str | None = None
    error: Literal["zk_wrap_missing"]
    kv: int
    device_key: str


# ---- zero-knowledge vault (canopy-web#1550; apps/agents/credentials/zk.py) ----------------
