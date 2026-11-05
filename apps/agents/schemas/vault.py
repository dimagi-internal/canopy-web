from __future__ import annotations

import datetime as dt
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from apps.common.schemas import StrictModel

ZkWrapKind = Literal["device", "member", "recovery"]


class ZkWrapIn(StrictModel):
    """One wrap of the vault key. `target` is a device key id (`device`), a user
    id (`member`), or null (`recovery`). `wrap` is the JSON object of
    docs/vault/zk-v1/zk_v1_reference.py §3 / §5 — shape-checked, never opened."""

    kind: ZkWrapKind
    target: str | None = None
    wrap: dict[str, Any]


class ZkValueIn(StrictModel):
    name: str = Field(max_length=120)
    #: `zk1:` + b64u(nonce12 || AES-GCM ciphertext+tag) — or, on PUT /values
    #: only, `zks1:` sealed to recipients (§6).
    value_zk: str
    #: Compare-and-swap (PUT /values only): lowercase hex SHA-256 of the
    #: `value_zk` the caller read and is replacing — a settle sends it so a
    #: value canopy-web minted meanwhile is never overwritten. "" = expect no
    #: value stored. Any mismatch refuses the whole request (409
    #: `zk_value_changed`); omitted/null = unconditional.
    expect: str | None = None


class ZkEnableIn(StrictModel):
    kv: int = 1
    wraps: list[ZkWrapIn]
    values: list[ZkValueIn] = Field(default_factory=list)


class ZkValuesIn(StrictModel):
    kv: int
    values: list[ZkValueIn]


class ZkWrapsIn(StrictModel):
    kv: int
    wraps: list[ZkWrapIn]


class ZkRotateIn(StrictModel):
    new_kv: int
    wraps: list[ZkWrapIn]
    values: list[ZkValueIn] = Field(default_factory=list)


class ZkWrapOut(StrictModel):
    kind: ZkWrapKind
    kv: int
    #: Device key id, user id, or null for the recovery wrap.
    target: str | None = None
    #: Who holds it, for a person to read ("box · label", an email).
    label: str = ""
    #: Device wraps: the ECDH thumbprint bound into the wrap.
    ecdh_jkt: str = ""
    #: False once the holder no longer qualifies (key revoked, admin removed):
    #: the wrap still opens this kv, so rotate.
    current: bool = True
    #: The holder's CURRENT public key (device ECDH key / person's keyset) —
    #: what rotate re-wraps to. Null for recovery or a holder that is gone.
    public_jwk: dict[str, Any] | None = None
    #: Device wraps: the same key under the name the device-key rows use.
    ecdh_public_jwk: dict[str, Any] | None = None
    wrap: dict[str, Any]
    created_by_email: str = ""
    created_at: dt.datetime


class ZkTargetOut(StrictModel):
    """A principal with no wrap at the current kv. `public_jwk` is what to wrap
    to: the device's ECDH key, or the person's keyset key."""

    kind: Literal["device", "member"]
    target: str
    public_jwk: dict[str, Any]
    #: Devices: the same key as `public_jwk`, under the device-key rows' name.
    ecdh_public_jwk: dict[str, Any] | None = None
    ecdh_jkt: str = ""
    runner: str = ""
    runner_name: str = ""
    label: str = ""
    device_kind: str = ""
    email: str = ""
    #: A workspace vault's page only, for a machine: whose it is, which agents this vault
    #: serves that it runs, and why it waits (`unendorsed`: its person never confirmed it,
    #: so no keeper will pass it the key — an admin's grant is the way in).
    person_email: str = ""
    serves: list[str] = Field(default_factory=list)
    waiting_reason: str = ""


class VaultMemberOut(StrictModel):
    user_id: int
    email: str
    #: "owner" | "admin" | "member" — the highest any source gives them (the agent's roles;
    #: owner only from owning the linked agent or workspace).
    role: str
    #: Why they are a member: "direct", "agent:<slug>", "workspace_owners:<slug>", …
    sources: list[str]
    #: They have a vault key of their own, so a copy of this vault's key can reach them.
    has_vault_key: bool
    #: They hold a copy of the vault's current key. A member with a vault key and no
    #: copy is WAITING: a keeper or a person who holds it passes it on.
    has_copy: bool = False


class VaultHoldersOut(StrictModel):
    """Who can pass the vault's key on to someone waiting."""

    #: The vault has a key yet (a zero-knowledge vault at kv ≥ 1).
    has_key: bool = False
    #: Members who hold a copy: each can grant with one passkey touch, and is emailed
    #: when someone has waited 15 minutes.
    people: list[str] = []
    #: Runners holding a copy that pass it on by themselves, every two minutes while online.
    keepers: list[str] = []


class VaultAttachmentOut(StrictModel):
    id: int
    #: Exactly one: the agent it serves, the workspace whose agents (and those below) it
    #: serves, the project whose member agents it serves, or the cloud runner whose own
    #: Claude credentials it holds (its id).
    agent: str | None = None
    workspace: str | None = None
    runner: str | None = None
    runner_name: str | None = None
    #: A project's address, `<workspace>/<handle>`: the vault serves its CURRENT member
    #: agents that lie in the vault's tree.
    project: str | None = None
    #: A project's member agents (slugs) the vault serves now ...
    served: list[str] = []
    #: ... and the ones it does not, because they lie outside the vault's tree (an agent of
    #: a sibling workspace that joined through a share).
    not_served: list[str] = []
    #: The project itself lies outside the vault's tree now, so it serves nobody.
    project_outside_tree: bool = False
    #: A runner's standing: "has" a copy of the vault's key, is "waiting" for one (a
    #: holder's passkey gives it), or "none" (no approved key it could hold one with).
    #: "" when it is not a runner, or the vault has no key yet.
    access: str = ""
    #: Where that runner's key lives: its ECDH key's store ("aws-kms", "kms-broker",
    #: "secure-enclave", "tpm", "software"), "" when it has none.
    key_place: str = ""


class WorkspaceVaultOut(StrictModel):
    vault_id: str
    workspace: str
    name: str
    #: Its key version; 0 = no key yet.
    kv: int
    #: Names only — never a value.
    secrets: list[str]
    attachments: list[VaultAttachmentOut]
    members: list[VaultMemberOut]
    you: str | None
    holders: VaultHoldersOut = VaultHoldersOut()


class VaultCreateIn(StrictModel):
    name: str


class VaultAttachIn(StrictModel):
    agent: str | None = None
    workspace: str | None = None
    #: A project's address, `<workspace>/<handle>`.
    project: str | None = None
    #: A cloud runner's id: the vault then holds that box's Claude credentials.
    runner: str | None = None


class VaultAgentLinkOut(StrictModel):
    linked: bool
    #: The role the link gives everyone with full access who is not an owner or admin.
    role: str


class VaultMembersOut(StrictModel):
    vault_id: str
    kind: str
    name: str
    agent_link: VaultAgentLinkOut
    members: list[VaultMemberOut]
    #: The caller's own role, or null when they are not a member.
    you: str | None
    holders: VaultHoldersOut = VaultHoldersOut()


class VaultMemberIn(StrictModel):
    email: str
    #: "admin" or "member" ("manage" / "use", the earlier words, still accepted).
    role: str = "member"


class VaultAgentLinkIn(StrictModel):
    linked: bool
    #: What the link gives everyone with full access who is not the agent's owner or an admin.
    role: str = "member"


class VaultKeeperPinsOut(StrictModel):
    """The keepers' shared record of whose key they trusted: a `zk1` value under the
    vault key at `kv`, bound to the name `~keeper-pins`. Empty `value` until a keeper
    first writes one. Write back over `version`."""

    value: str = ""
    kv: int = 0
    version: int = 0


class VaultKeeperPinsIn(StrictModel):
    kv: int
    #: The `version` the keeper read; 409 `keeper_pins_stale` if it moved since.
    expected_version: int
    value: str


class VaultKeeperAgentOut(StrictModel):
    #: The agent's slug, or a workspace vault's name.
    agent: str
    #: What the key is bound to: the agent's id, or the workspace vault's binding id.
    agent_id: str
    kv: int
    #: This box's device key holding the wrap, and the wrap itself.
    device_key: str
    wrap: dict[str, Any]
    pending: list[ZkTargetOut]
    endorsements: list[ZkEndorsementOut]
    #: Where the keeper posts its copies.
    grant_path: str = ""
    #: Set for a workspace vault.
    vault_id: str = ""
    #: The keepers' shared record, and where to write it back.
    pins: VaultKeeperPinsOut = VaultKeeperPinsOut()
    pins_path: str = ""


class VaultKeeperRecipientOut(StrictModel):
    kind: str
    target: str
    public_jwk: dict[str, Any]
    runner_name: str = ""


class VaultKeeperDelegatedOut(StrictModel):
    """A credential a person delegated to an agent, sealed to this box, that a machine
    which runs the agent is still waiting for. The box opens it, checks each waiting
    machine's endorsement, and re-seals it to `recipients` (all of them that it keeps)."""

    agent: str
    agent_id: str
    name: str
    #: This box's key the value is sealed to.
    device_jkt: str
    value_zk: str
    #: The digest of `value_zk`, to send back as `expect`.
    expect: str
    #: Everyone it may be sealed to now (the machines that run the agent, and its person).
    recipients: list[VaultKeeperRecipientOut]
    #: The targets (ECDH thumbprints) of the machines still waiting.
    waiting: list[str]
    endorsements: list[ZkEndorsementOut]
    reseal_path: str


class VaultKeeperOut(StrictModel):
    agents: list[VaultKeeperAgentOut]
    delegated: list[VaultKeeperDelegatedOut] = Field(default_factory=list)


class RunnerDelegatedCheckRowIn(StrictModel):
    repo: str = ""
    ok: bool
    detail: str = ""


class RunnerDelegatedCheckIn(StrictModel):
    """What a machine found probing a credential delegated to `agent` (vault entry `name`)."""

    agent: str
    name: str
    checks: list[RunnerDelegatedCheckRowIn] = Field(default_factory=list)
    #: What the provider said about the token's expiry (GitHub's header), if anything.
    provider_expires_at: dt.datetime | None = None
    #: The provider refused it outright (GitHub 401): it no longer works.
    error: str = ""


class RunnerDelegatedCheckOut(StrictModel):
    #: The entry's sentence after the report ("… no longer works — replace it").
    label: str


class DelegatedResealIn(StrictModel):
    #: The value re-sealed (`zks1:`) to the machines that run the agent.
    value_zk: str
    #: `expect` from the keeper's work: the digest of the value this box opened.
    expect: str


class DelegatedResealOut(StrictModel):
    #: The machines that can open it now.
    machines: list[str]


class VaultKeeperGrantOut(StrictModel):
    granted: int


class ZkStateOut(StrictModel):
    agent: str
    #: The Agent primary key as a string — bound into every AAD and HKDF info.
    agent_id: str
    enabled: bool
    kv: int
    wraps: list[ZkWrapOut]
    pending: list[ZkTargetOut]
    rotation_recommended: bool = False
    #: name → `zk1:` ciphertext (or `zks1:` sealed to recipients), only with
    #: `?values=true` (each read is logged).
    values: dict[str, str] | None = None
    #: Names of server-minted values still sealed to recipients (`zks1`),
    #: waiting for a vault-key holder to settle them into `zk1`. kv 0 = the
    #: agent is zero-knowledge from birth and has no vault key yet.
    sealed: list[str] = []
    #: Names of credentials a person delegated to the agent: sealed to the machines that
    #: run it, never under the vault key, so no member reads, settles or rotates them.
    delegated: list[str] = []
    #: The keepers' record (`~keeper-pins` under the vault key): a browser that grants
    #: someone records the key it granted to, so keepers trust it from then on.
    pins: VaultKeeperPinsOut = VaultKeeperPinsOut()


class ZkValuesOut(StrictModel):
    kv: int
    names: list[str]


class ZkRecipientOut(StrictModel):
    """A principal a `zks1` value may be sealed to (zk-v1 §6). `target` is what
    the seal binds: a device's ECDH thumbprint, or a member's user id."""

    kind: Literal["device", "member"]
    target: str
    label: str = ""
    public_jwk: dict[str, Any]
    #: RFC 7638 thumbprint of `public_jwk` — what an endorsement signs.
    key_jkt: str


class ZkEndorsementOut(StrictModel):
    """A person's §7 signature that `key_jkt` may read this agent."""

    kind: Literal["device", "member"]
    target: str
    key_jkt: str
    by: str
    by_email: str = ""
    by_jkt: str
    by_public_jwk: dict[str, Any] | None = None
    sig: str
    #: The agent the signature was made for, when it is not the one being read
    #: (a keeper's workspace-vault work verifies an attached agent's endorsement).
    agent_id: str = ""


class ZkRecipientsOut(StrictModel):
    """Everything a writer needs to seal a value without the vault key: who may
    read the agent, and who vouched for each. Public keys and signatures only."""

    agent: str
    agent_id: str
    kv: int
    recipients: list[ZkRecipientOut]
    endorsements: list[ZkEndorsementOut]
    #: Declared names (Agent.runtime_secrets), and which of them hold a value now.
    declared: list[str]
    stored: list[str]


class ZkEndorsementIn(StrictModel):
    kind: Literal["device", "member"]
    target: str = Field(max_length=64)
    key_jkt: str = Field(max_length=64)
    #: Thumbprint of the signer's vault key — must be the caller's own.
    by_jkt: str = Field(max_length=64)
    sig: str = Field(max_length=100)


class ZkEndorsementsIn(StrictModel):
    endorsements: list[ZkEndorsementIn]


class ZkEndorsementsOut(StrictModel):
    stored: int


class SecretRequestIn(StrictModel):
    #: A secret name the agent declares in its vault.
    name: str = Field(max_length=200)
    #: One line for the person answering: why the agent needs it.
    reason: str = Field(default="", max_length=300)


class SecretRequestOut(StrictModel):
    """A request carries NO value — a name, a reason and a status."""

    id: str
    #: The same id, under the name the chat's seal card is found by.
    secret_request_id: str
    agent: str
    name: str
    reason: str = ""
    #: open | fulfilled | cancelled | expired
    status: str
    #: Whether the agent holds any value for `name` now.
    has_value: bool
    session_id: str | None = None
    created_at: datetime
    expires_at: datetime
    fulfilled_at: datetime | None = None
    fulfilled_by_email: str = ""
    #: What to tell the person, for the agent to relay.
    message: str = ""


class SecretRequestValueIn(StrictModel):
    #: The agent's current vault-key version (409 `zk_kv_mismatch` otherwise).
    kv: int
    #: `zks1:` sealed in the answerer's browser (zk-v1 §6). Nothing else.
    value_zk: str


class BootstrapReportIn(StrictModel):
    """Posted BY a box at the end of its bootstrap pass, per agent.

    Booleans the box OBSERVED, not configuration it read back. `mailbox_ok`
    specifically means a gmail call was attempted and succeeded — the one thing
    a credentials screen can never tell you (2026-09-07)."""

    runner_name: str
    client_creds_ok: bool = False
    mailbox_ok: bool = False
    gog_client: str = ""
    #: The client the agent's turns present, and whether the mailbox works under
    #: it. `turn_ready` is None from a box that did not check — never False,
    #: which would report a healthy agent as broken.
    turn_client: str = ""
    turn_ready: bool | None = None
    #: Did the agent's secrets materialize (`canopy cred sync` writing its .env)? None
    #: from a box that did not say — never False.
    env_ok: bool | None = None
    detail: str = ""


class BootstrapReportOut(StrictModel):
    runner_name: str = ""
    client_creds_ok: bool = False
    mailbox_ok: bool = False
    gog_client: str = ""
    turn_client: str = ""
    turn_ready: bool | None = None
    env_ok: bool | None = None
    detail: str = ""
    reported_at: datetime | None = None
