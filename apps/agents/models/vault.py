"""Agent models: vault."""

import uuid

from django.conf import settings
from django.db import models

from .agent import Agent
from .members import NamedMember


class VaultEntryAccount(models.Model):
    """Whose account a vault entry is, as clear columns beside its ciphertext — the same
    fields on an agent vault's entries (`AgentCredential`) and a workspace vault's
    (`VaultSecret`). Read through `credentials.accounts`, never the columns directly.

    - `agent`: the agent's own credential (its own login or account). Most entries.
    - `delegated`: a standing credential from a PERSON's own account, which they gave
      the agent to use (Jonathan's GitHub token for ACE). Sealed so that only the
      cloud boxes that run the agent, and machines of that person's own, can open it
      (`accounts.delegated_devices`), never
      under the vault key, and readable by no person through canopy: not the agent's
      admins, not even the person it came from (they replace or withdraw it).
      canopy gives it no expiry of its own; `provider_expires_at` is what the provider
      said, shown as information.

    """

    AGENT = "agent"
    DELEGATED = "delegated"
    ACCOUNT_CHOICES = [(AGENT, "The agent's own"), (DELEGATED, "Delegated by a person")]
    PROVIDERS = ("github", "google", "salesforce", "anthropic", "other")

    account = models.CharField(max_length=16, choices=ACCOUNT_CHOICES, default=AGENT)
    #: The person whose account it is. Required when delegated.
    delegated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True,
                                     blank=True, related_name="+")
    #: When the account is an AGENT's own and another agent uses it (Eva's Salesforce
    #: user, used by Echo): that agent. To it the entry is its own.
    delegated_by_agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, null=True,
                                           blank=True, related_name="+")
    #: How a box injects and probes it: github / salesforce / anthropic / other.
    provider = models.CharField(max_length=32, blank=True, default="")
    #: The external login, never a secret: `{login, user_id, name}` for GitHub.
    identity = models.JSONField(default=dict, blank=True)
    #: One line, shown everywhere ("ACE opens PRs on ace-web").
    purpose = models.CharField(max_length=200, blank=True, default="")
    #: What it reaches, as declared. Shown, not enforced: the provider enforces it.
    scopes = models.JSONField(default=list, blank=True)
    #: What the provider reported at a check. Information only: canopy never expires or
    #: refuses an entry on a date of its own.
    provider_expires_at = models.DateTimeField(null=True, blank=True)
    #: The last probe (`[{repo, ok, detail}]` for GitHub), when, and who ran it:
    #: "server" (when it was given), or the name of the machine that ran it.
    checks = models.JSONField(default=list, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    checked_by = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        abstract = True

    @classmethod
    def account_constraints(cls, prefix: str) -> list:
        """A delegated entry names its person and provider; the agent's own names neither."""
        delegated = models.Q(account=cls.DELEGATED)
        return [
            models.CheckConstraint(
                condition=models.Q(account__in=[cls.AGENT, cls.DELEGATED]),
                name=f"{prefix}_account_kind"),
            models.CheckConstraint(
                condition=~delegated | (models.Q(delegated_by__isnull=False) & ~models.Q(provider="")),
                name=f"{prefix}_delegated_names_its_person"),
            models.CheckConstraint(
                condition=delegated | (models.Q(delegated_by__isnull=True)
                                       & models.Q(delegated_by_agent__isnull=True)),
                name=f"{prefix}_own_names_no_person"),
        ]


class AgentCredential(VaultEntryAccount):
    """One named secret for one agent, as ciphertext canopy-web cannot open.


    canopy-web is the agent secret store, so standing up an agent does not
    require a vault someone must be granted elsewhere.

    Zero-knowledge only: canopy-web stores `value_zk` and holds no key that opens
    it. A machine that runs the agent opens it with its own device key, a person
    with their vault key, in their browser. There is no custodial copy.

    NAMED SLOTS, not fixed columns: RunnerCredential has five columns because a
    runner has five credentials; ACE declares 45 refs and echo 12.

    Deliberately carries NO `optional`/`env`/`path` metadata. That is the
    DECLARATION's job, and it lives in the agent's vault declaration
    (Agent.runtime_secrets, written by `canopy agent vault`). Storing shape here too is the
    two-writers mistake that produced the gog_client drift — three copies of one
    fact, two of them wrong.
    """

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="credentials")
    #: Matches a name in the agent's declared secrets (Agent.runtime_secrets).
    name = models.CharField(max_length=120)
    #: Zero-knowledge (canopy-web#1550), the only way a value is stored: `zk1:`
    #: AES-GCM ciphertext under the agent's vault key at version `kv`, or `zks1`
    #: sealed to each reader before the vault key exists (kv 0). canopy-web holds
    #: no key that opens either.
    value_zk = models.TextField(blank=True, default="")
    kv = models.PositiveIntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["agent_id", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["agent", "name"], name="one_value_per_agent_secret"
            ),
            *VaultEntryAccount.account_constraints("agentcred"),
        ]

    def __str__(self) -> str:
        return f"{self.agent.slug}:{self.name}"


class AgentCredentialRead(models.Model):
    """One read of one agent credential's VALUE — the read log (canopy-web#1538).

    Written by `/credentials/resolve`, one row per ref handed out, so "who read
    `gog-token`, from which box, during which turn" is a query rather than a
    trawl through the event log. `name` is a plain string, not a FK: deleting a
    credential must not erase the record that it was read.
    """

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="credential_reads")
    name = models.CharField(max_length=120)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    #: The box the read came from, when known (the device key that proved it,
    #: or an `X-Canopy-Runner` header naming a runner the caller owns).
    runner = models.ForeignKey(
        "harness.Runner", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    #: `runner` (a box that runs the agent), `admin` (a human resolving by hand) or
    #: `license` (a license holder's direct turn, `licenses.py`).
    via = models.CharField(max_length=16, blank=True, default="")
    #: The license a `license` read went out under.
    license = models.ForeignKey("AgentLicense", on_delete=models.SET_NULL, null=True,
                                blank=True, related_name="credential_reads")
    #: The `X-Canopy-Turn-Id` header, when the caller sent one.
    turn_id = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agent", "name", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.agent_id}:{self.name}@{self.created_at:%Y-%m-%d %H:%M}"


class AgentCredentialAlert(models.Model):
    """A read of an agent's secrets that looked wrong, and whether its owner was
    told (`apps/agents/credentials/vault_alerts.py`).

    At most one row per (agent, kind) per hour: a repeat inside the hour bumps
    `occurrences` on the open row instead of notifying again. Shown beside the
    credential read history."""

    NO_TURN, UNDECLARED, BURST, DEVICE_PROOF = "no-turn", "undeclared-ref", "burst", "device-proof"
    ZK_WRAP_MISSING = "zk-wrap-missing"
    #: A NO_TURN read whose caller labelled it a verification check. Its own kind so
    #: the hourly debounce of one never swallows the other: a labelled check cannot
    #: use up the hour's alert for an unlabelled read after it.
    NO_TURN_CHECK = "no-turn-check"
    KIND_CHOICES = [
        (NO_TURN, "Read outside a turn, not by an owner or admin"),
        (NO_TURN_CHECK, "Read outside a turn, labelled as a verification check"),
        (UNDECLARED, "Read of a ref the agent does not declare"),
        (BURST, "Burst of reads"),
        (DEVICE_PROOF, "Resolve refused: bad or missing device proof"),
        (ZK_WRAP_MISSING, "Resolve refused: the device holds no vault-key wrap"),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="credential_alerts")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    #: Human-readable: what was seen. Never a value.
    detail = models.CharField(max_length=500, blank=True, default="")
    #: The refs involved, when the kind is about refs.
    refs = models.JSONField(default=list, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    runner = models.ForeignKey(
        "harness.Runner", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    turn_id = models.CharField(max_length=120, blank=True, default="")
    #: Who asked, as the request said and canopy-web resolved it — enough to name the
    #: process without SSH-ing to the box (2026-10-28: an alert named a turn id its admin
    #: could not open, and nothing else): the client and User-Agent, the session headers,
    #: the CLI's reason for sending no device proof, and what the named turn is (agent,
    #: origin, chat session, who started it). Never a value. `vault_alerts.request_context`.
    context = models.JSONField(default=dict, blank=True)
    #: The same context for each repeat inside the hour, newest last (capped), so a
    #: debounced repeat is still traceable instead of only a count.
    repeats = models.JSONField(default=list, blank=True)
    #: How many times it happened inside this row's hour (1 = just this one).
    occurrences = models.PositiveIntegerField(default=1)
    #: What the notification did: {"emailed": [...], "pushed": n, ...}.
    notified = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agent", "kind", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.agent_id}:{self.kind}@{self.created_at:%Y-%m-%d %H:%M}"


class AgentVaultWrap(models.Model):
    """The agent's vault key (VK) at version `kv`, wrapped to ONE principal
    (Canopy Vault Phase 2, canopy-web#1550; formats in
    docs/vault/zk-v1/zk_v1_reference.py).

    * `device` — ECDH-ES to a RunnerDeviceKey's `ecdh_public_jwk`;
    * `member` — ECDH-ES to a person's UserVaultKeyset public key;
    * `recovery` — under a 256-bit code shown once to whoever enabled or rotated.

    `wrap` is public data (it opens only with the principal's private key), so
    it is stored and served as-is. canopy-web checks its SHAPE and never tries
    to open it. One wrap per target per kv; rotating replaces them all."""

    DEVICE, MEMBER, RECOVERY = "device", "member", "recovery"
    KIND_CHOICES = [(DEVICE, "Device"), (MEMBER, "Member"), (RECOVERY, "Recovery code")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="vault_wraps")
    kv = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    device_key = models.ForeignKey(
        "harness.RunnerDeviceKey", on_delete=models.CASCADE, null=True, blank=True,
        related_name="vault_wraps",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="+",
    )
    wrap = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["agent_id", "kv", "kind", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "kv", "device_key"], name="one_device_wrap_per_kv",
                                    condition=models.Q(kind="device")),
            models.UniqueConstraint(fields=["agent", "kv", "user"], name="one_member_wrap_per_kv",
                                    condition=models.Q(kind="member")),
            models.UniqueConstraint(fields=["agent", "kv"], name="one_recovery_wrap_per_kv",
                                    condition=models.Q(kind="recovery")),
        ]

    def __str__(self) -> str:
        return f"{self.agent_id}:kv{self.kv}:{self.kind}"


class AgentVaultEndorsement(models.Model):
    """A person's signature saying "this public key may read this agent" (zk-v1
    §7).

    The publicly checkable twin of a wrap: made in the endorser's browser when
    they wrap the vault key to a recipient (or confirm it), and checked by a
    WRITER who holds no vault key before sealing a `zks1` value. A writer seals
    only to recipients endorsed by a key it already trusts, so canopy-web cannot
    add a recipient of its own by inserting a key row. canopy-web checks the
    signature on write (so junk is refused) and serves the rows as-is.

    `target` is as in §6: a device's ECDH thumbprint, or a member's user id.
    One endorsement per (agent, kind, target, endorser); re-endorsing replaces it."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="vault_endorsements")
    kind = models.CharField(max_length=10)  # "device" | "member"
    target = models.CharField(max_length=64)
    #: RFC 7638 thumbprint of the recipient's public JWK, as signed.
    key_jkt = models.CharField(max_length=64)
    endorser = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    #: Thumbprint of the endorser's vault key when they signed.
    endorser_jkt = models.CharField(max_length=64)
    #: b64u IEEE P1363 r||s.
    sig = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["agent_id", "kind", "target", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "kind", "target", "endorser"],
                                    name="one_endorsement_per_target_per_endorser"),
        ]

    def __str__(self) -> str:
        return f"{self.agent_id}:{self.kind}:{self.target} by {self.endorser_id}"


class Vault(models.Model):
    """A vault: a named set of secrets with its OWN membership.

    Membership is first-class: who may open a vault is its `VaultMember` rows
    (people added by name) plus its `VaultLink` rows (rules that stand for a set
    of people canopy already knows, such as everyone with full access to an
    agent), resolved by `credentials.vault_members.members`. An agent's vault
    starts linked to its agent, so giving someone the agent gives them its vault;
    a manager can add people directly, link more, or drop the link.

    An AGENT vault's secrets and wraps are still stored on the agent
    (`AgentCredential`, `AgentVaultWrap`); this row holds who may open them. A
    WORKSPACE vault (any number per workspace) keeps its own: `VaultSecret`,
    `VaultKeyWrap`, and the agents it serves are its `VaultAttachment` rows."""

    AGENT, WORKSPACE = "agent", "workspace"
    KIND_CHOICES = [(AGENT, "Agent vault"), (WORKSPACE, "Workspace vault")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="vaults")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    name = models.CharField(max_length=120)
    agent = models.OneToOneField(Agent, on_delete=models.CASCADE, null=True, blank=True, related_name="vault")
    #: A WORKSPACE vault's own key version (always zero-knowledge; 0 = no key yet). An
    #: agent vault's key version stays on the agent (`Agent.zk_kv`).
    kv = models.PositiveIntegerField(default=0)
    #: The keepers' shared record of whose key they have trusted, person by person
    #: (`credentials/vault_keeper.py` § the keepers' record): a `zk1` value under the
    #: vault key at `keeper_pins_kv`, bound to the name `~keeper-pins`. canopy-web
    #: stores it and can neither read nor forge it; `keeper_pins_version` goes up on
    #: every write, so two keepers cannot overwrite each other.
    keeper_pins_zk = models.TextField(blank=True, default="")
    keeper_pins_kv = models.PositiveIntegerField(default=0)
    keeper_pins_version = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["workspace_id", "kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "name"], name="one_workspace_vault_per_name",
                                    condition=models.Q(kind="workspace")),
            models.CheckConstraint(condition=(models.Q(kind="agent", agent__isnull=False)
                                              | models.Q(kind="workspace", agent__isnull=True)),
                                   name="vault_agent_iff_agent_kind"),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


class VaultSecret(VaultEntryAccount):
    """One value in a WORKSPACE vault: `zk1:` ciphertext under the vault's key, never
    anything canopy-web can read (formats: docs/vault/zk-v1/zk_v1_reference.py, bound to
    `credentials.workspace_vaults.binding_id`)."""

    vault = models.ForeignKey("Vault", on_delete=models.CASCADE, related_name="secrets")
    name = models.CharField(max_length=200)
    value_zk = models.TextField()
    kv = models.PositiveIntegerField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vault_id", "name"]
        constraints = [models.UniqueConstraint(fields=["vault", "name"], name="one_vault_secret_per_name"),
                       *VaultEntryAccount.account_constraints("vaultsecret")]

    def __str__(self) -> str:
        return f"{self.vault_id}:{self.name}"


class VaultKeyWrap(models.Model):
    """A WORKSPACE vault's key at version `kv`, wrapped to one principal — the
    workspace-vault twin of `AgentVaultWrap` (same kinds, same one-per-target rule)."""

    DEVICE, MEMBER, RECOVERY = "device", "member", "recovery"

    vault = models.ForeignKey("Vault", on_delete=models.CASCADE, related_name="wraps")
    kv = models.PositiveIntegerField()
    kind = models.CharField(max_length=10)
    device_key = models.ForeignKey("harness.RunnerDeviceKey", on_delete=models.CASCADE, null=True, blank=True,
                                   related_name="workspace_vault_wraps")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                             related_name="+")
    wrap = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vault_id", "kv", "kind", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["vault", "kv", "device_key"], name="one_device_vault_wrap_per_kv",
                                    condition=models.Q(kind="device")),
            models.UniqueConstraint(fields=["vault", "kv", "user"], name="one_member_vault_wrap_per_kv",
                                    condition=models.Q(kind="member")),
            models.UniqueConstraint(fields=["vault", "kv"], name="one_recovery_vault_wrap_per_kv",
                                    condition=models.Q(kind="recovery")),
        ]

    def __str__(self) -> str:
        return f"{self.vault_id}:kv{self.kv}:{self.kind}"


class VaultAttachment(models.Model):
    """What a WORKSPACE vault serves: one agent, or every agent in a workspace and
    below it (`workspace`, a subtree), or every CURRENT member agent of a project
    (`project`; no list is stored, so joining the project gives the vault and leaving
    it takes it away) — a turn of an agent reads every vault attached to it, layered
    under its own (`workspace_vaults.layers_for`) — or one cloud RUNNER, whose own
    Claude credentials it holds (`workspace_vaults.runner_layers`)."""

    vault = models.ForeignKey("Vault", on_delete=models.CASCADE, related_name="attachments")
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="vault_attachments")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="vault_attachments")
    runner = models.ForeignKey("harness.Runner", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="vault_attachments")
    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="vault_attachments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vault_id", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(agent__isnull=False, workspace__isnull=True, runner__isnull=True, project__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=False, runner__isnull=True, project__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=True, runner__isnull=False, project__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=True, runner__isnull=True, project__isnull=False)),
                name="vault_attachment_one_target"),
            models.UniqueConstraint(fields=["vault", "agent"], name="one_vault_attachment_per_agent",
                                    condition=models.Q(agent__isnull=False)),
            models.UniqueConstraint(fields=["vault", "workspace"], name="one_vault_attachment_per_subtree",
                                    condition=models.Q(workspace__isnull=False)),
            models.UniqueConstraint(fields=["vault", "runner"], name="one_vault_attachment_per_runner",
                                    condition=models.Q(runner__isnull=False)),
            models.UniqueConstraint(fields=["vault", "project"], name="one_vault_attachment_per_project",
                                    condition=models.Q(project__isnull=False)),
        ]

    def __str__(self) -> str:
        return f"{self.vault_id} -> {self.agent_id or self.workspace_id or self.project_id or self.runner_id}"


class VaultMember(NamedMember):
    """A person added to a vault by name — a direct membership, kept until an
    admin removes it, whatever happens to any agent the vault is linked to.

    A vault's roles are the agent's: `owner`, `admin`, `member` (no contacts). Owner
    is never stored: it comes only from owning the linked agent (or the workspace,
    for a workspace vault). A row or a link gives `admin` or `member`."""

    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vault_memberships")

    class Meta:
        ordering = ["vault_id", "added_at"]
        constraints = [models.UniqueConstraint(fields=["vault", "user"], name="one_vault_member_row")]

    def __str__(self) -> str:
        return f"{self.vault_id}:{self.user_id} ({self.role})"


class VaultLink(models.Model):
    """A rule that makes a set of people members of a vault, re-read on every
    question so it follows the set as it changes:

    * `agent` — everyone with FULL access to `agent` (`access.roster`): its owner as
      `owner`, its admins as `admin`, everyone else as `role`;
    * `workspace_owners` / `workspace_admins` — the owners (as `owner`) or the admins
      (as `role`) of `workspace`, inherited down the tree as those roles are."""

    AGENT, WORKSPACE_OWNERS, WORKSPACE_ADMINS = "agent", "workspace_owners", "workspace_admins"
    #: Everyone with full access to an agent this (workspace) vault is attached to, by
    #: itself or through a workspace, and the people of a project it is attached to.
    ATTACHED = "attached"
    KIND_CHOICES = [(AGENT, "Full access to an agent"), (WORKSPACE_OWNERS, "Owners of a workspace"),
                    (WORKSPACE_ADMINS, "Admins of a workspace"), (ATTACHED, "What the vault is attached to")]

    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="links")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, null=True, blank=True, related_name="vault_links")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="vault_links")
    role = models.CharField(max_length=10, choices=VaultMember.ROLE_CHOICES, default=VaultMember.MEMBER)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vault_id", "created_at"]
        constraints = [
            models.CheckConstraint(condition=(models.Q(kind="agent", agent__isnull=False, workspace__isnull=True)
                                              | models.Q(kind="attached", agent__isnull=True, workspace__isnull=True)
                                              | (~models.Q(kind__in=["agent", "attached"])
                                                 & models.Q(agent__isnull=True, workspace__isnull=False))),
                                   name="vault_link_target_matches_kind"),
            models.UniqueConstraint(fields=["vault", "kind", "agent"], name="one_agent_link_per_vault",
                                    condition=models.Q(kind="agent")),
            models.UniqueConstraint(fields=["vault", "kind", "workspace"], name="one_workspace_link_per_vault",
                                    condition=~models.Q(kind__in=["agent", "attached"])),
            models.UniqueConstraint(fields=["vault"], name="one_attached_link_per_vault",
                                    condition=models.Q(kind="attached")),
        ]

    def __str__(self) -> str:
        return f"{self.vault_id}:{self.kind}"


class AgentSecretRequest(models.Model):
    """An agent asking for one of its declared secrets.
   A request carries NO value,
    ever: it is what the chat's seal card answers. The answer is a `zks1` value
    sealed in the answerer's browser, stored through `zk.set_values`.

    Who may answer is `secret_requests.may_answer`: an agent admin (fill or
    replace), or a person who can write to the session it was asked from (fill
    a MISSING value only). Never an agent's own login."""

    OPEN, FULFILLED, CANCELLED = "open", "fulfilled", "cancelled"
    STATUS_CHOICES = [(OPEN, "Open"), (FULFILLED, "Fulfilled"), (CANCELLED, "Cancelled")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="secret_requests")
    name = models.CharField(max_length=200)
    reason = models.CharField(max_length=300, blank=True, default="")
    #: The chat it was asked from (the turn's session), where the card renders.
    session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="secret_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=OPEN)
    fulfilled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+")
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agent", "name", "status"])]

    def __str__(self) -> str:
        return f"{self.agent_id}:{self.name} ({self.status})"


class AgentGoogleMint(models.Model):
    """One "Connect Google mailbox" sign-in, finished on one of the agent's runners
    (`apps/agents/credentials/google_mint.py`, docs/architecture/vault.md).

    canopy-web never holds the token: the runner makes the PKCE verifier and keeps
    it, canopy-web builds the consent link from its challenge and relays the code
    Google sends back, and the runner exchanges code + verifier with the OAuth
    client's secret from the agent's vault, encrypts the token for the agent and
    posts only ciphertext. A code is worthless without the verifier, so canopy-web,
    which sees only the code, cannot redeem it.

    POLLED by the runner on its existing tick (the `mint/claim` read), like the
    runner's own Claude sign-in (`harness.RunnerMint`)."""

    REQUESTED = "requested"    # the runner owes a PKCE challenge
    AWAITING = "awaiting"      # the consent link is up; the person signs in
    COMPLETING = "completing"  # Google sent a code; the runner owes the exchange
    DONE, FAILED = "done", "failed"
    STATUS_CHOICES = [(REQUESTED, "requested"), (AWAITING, "awaiting consent"),
                      (COMPLETING, "completing"), (DONE, "done"), (FAILED, "failed")]
    FINISHED = (DONE, FAILED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="google_mints")
    runner = models.ForeignKey("harness.Runner", on_delete=models.CASCADE, related_name="google_mints")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=REQUESTED)
    #: A person connecting THEIR OWN Google account to the agent (a delegated credential,
    #: `credentials/accounts.py`), not the agent's own login. Null for the agent's own.
    delegated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True,
                                     blank=True, related_name="+")
    #: The account the consent must come from — for a delegated sign-in, the one the
    #: person named up front; "" = the agent's own rule (`google_oauth.expected_account`).
    expected_account = models.CharField(max_length=254, blank=True, default="")
    #: gog's service names a delegated sign-in asks for (`google_oauth.delegated_scopes`).
    services = models.JSONField(default=list, blank=True)
    #: A SESSION: the person lends their account for this many minutes (at most 60), from a
    #: page. Google is asked for no refresh token, so the token dies on its own; 0 = a
    #: standing delegation (connect once, withdraw in Settings).
    session_minutes = models.PositiveSmallIntegerField(default=0)
    #: How it ended, in the words the settings page reads (`?google=`): ok, denied,
    #: exchange-failed, no-refresh-token, no-email, wrong-account, no-client,
    #: zk-no-recipients, expired, interrupted.
    outcome = models.CharField(max_length=32, blank=True, default="")
    login_hint = models.CharField(max_length=254, blank=True, default="")
    code_challenge = models.CharField(max_length=128, blank=True, default="")
    authorize_url = models.TextField(blank=True, default="")
    #: The single-use code, held only until the runner takes it (read-and-clear).
    code = models.TextField(blank=True, default="")
    #: The Google account that consented (never a secret), once the runner says.
    account = models.CharField(max_length=254, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["runner", "status"]), models.Index(fields=["agent", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.agent_id} via {self.runner_id} ({self.status})"


class ZkGrantNotice(models.Model):
    """A wrap target the agent's vault key has not reached yet, and whether the
    people who hold that key were told (canopy-web#1550; `apps/agents/credentials/zk_notify.py`).

    One row per (agent, kind, target, kv): seen first at `first_seen_at`, told
    at most once (`notified_at`), closed (`cleared_at`) once the target is
    wrapped or stops qualifying. A target that needs a grant again at a later kv
    gets a new row."""

    #: Exactly one of the two: an agent's own vault, or a workspace vault.
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="zk_grant_notices")
    vault = models.ForeignKey("Vault", on_delete=models.CASCADE, null=True, blank=True,
                              related_name="grant_notices")
    kind = models.CharField(max_length=10)  # "device" | "member"
    #: The RunnerDeviceKey pk or the user id, as `zk.pending_targets` names it.
    target = models.CharField(max_length=64)
    kv = models.PositiveIntegerField()
    label = models.CharField(max_length=300, blank=True, default="")
    first_seen_at = models.DateTimeField()
    notified_at = models.DateTimeField(null=True, blank=True)
    #: What the notification did: {"emailed": [...], "pushed": n, ...}.
    notified = models.JSONField(default=dict, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-first_seen_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "kind", "target", "kv"], name="one_zk_grant_notice_per_target"),
            models.UniqueConstraint(fields=["vault", "kind", "target", "kv"],
                                    name="one_vault_grant_notice_per_target"),
            models.CheckConstraint(condition=(models.Q(agent__isnull=False, vault__isnull=True)
                                              | models.Q(agent__isnull=True, vault__isnull=False)),
                                   name="grant_notice_agent_xor_vault"),
        ]

    def __str__(self) -> str:
        return f"{self.agent_id or self.vault_id}:kv{self.kv}:{self.kind}:{self.target}"


class AgentPatRotation(models.Model):
    """One automatic (or on-demand) rotation of an agent's `canopy-pat`
    (`apps/agents/credentials/pat_rotation.py`).

    The new token is written to the agent's `canopy-pat` AgentCredential; the
    old one keeps working for the overlap (`revoke_after`, also set as its
    `expires_at` so it dies even if nothing revokes it) and is then revoked."""

    ROTATED, FAILED = "rotated", "failed"
    STATUS_CHOICES = [(ROTATED, "Rotated"), (FAILED, "Failed")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="pat_rotations")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    #: `scheduler` or `manual`.
    trigger = models.CharField(max_length=16, default="scheduler")
    detail = models.CharField(max_length=500, blank=True, default="")
    old_token = models.ForeignKey(
        "tokens.PersonalToken", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    new_token = models.ForeignKey(
        "tokens.PersonalToken", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    revoke_after = models.DateTimeField(null=True, blank=True)
    old_revoked_at = models.DateTimeField(null=True, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.agent_id}:pat-rotation:{self.status}@{self.created_at:%Y-%m-%d %H:%M}"


class AgentLoginRequest(models.Model):
    """An agent admin asking that a PERSON's existing canopy account become the agent's
    own login (`Agent.user`), waiting for that person to say yes.

    Linking an account makes it the agent: the agent's token signs in as it, and
    `mint_agent_canopy_token` / rotation mint tokens for it. Done to someone else's
    account without them, it would let an admin act as that person. So an account that
    has ever signed in interactively (`last_login` set — it may be a person's) is linked
    only when its own person accepts, in their own browser behind a passkey step-up
    (`agent_login.accept_link`). A fresh account made for the agent (never signed in)
    is linked by an admin directly, as before."""

    PENDING, ACCEPTED, DECLINED, WITHDRAWN = "pending", "accepted", "declined", "withdrawn"
    STATUS_CHOICES = [(PENDING, "Pending"), (ACCEPTED, "Accepted"), (DECLINED, "Declined"),
                      (WITHDRAWN, "Withdrawn")]

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="login_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="agent_login_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name="+")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent"], condition=models.Q(status="pending"),
                                    name="one_pending_login_request_per_agent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"login-request:{self.agent_id}->{self.user_id}:{self.status}"


class RepoIdentity(models.Model):
    """Which agent's GitHub identity an AGENTLESS repo turn borrows.

    A project turn (`Turn.project` = a repo name such as "canopy-web", no
    agent) runs as nobody, so `delegations.github_token_for_turn` refuses it —
    by design there is no shared fallback. A row here lifts that refusal for one
    repo: the turn runs as `agent`, using its owner's delegation (and refused
    exactly as that agent's own turns would be when the delegation is missing
    or expired). No row keeps the refusal.

    A row per REPO, not a field on `Agent`: the question is asked by repo name
    ("who does canopy-web's turn run as?"), one agent answers for several repos
    (Hal for canopy, canopy-web, ace-web and connect-labs), and a repo must have
    at most one answer. `repo_slug` unique is that rule in the database; a list
    on `Agent` could not stop two agents claiming the same repo.

    Moved here from the retired workbench project registry's
    `default_identity_agent` (agents/0037 copies the rows), so the lookup is a
    plain framework query instead of a resolver a product app registered.
    """

    repo_slug = models.CharField(
        max_length=100, unique=True,
        help_text="The repo name a project turn carries in Turn.project (e.g. canopy-web).",
    )
    agent = models.ForeignKey(
        Agent, on_delete=models.CASCADE, related_name="repo_identities",
        help_text="The agent whose owner's GitHub delegation the repo's agentless turns use.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["repo_slug"]
        verbose_name_plural = "repo identities"

    def __str__(self) -> str:  # pragma: no cover
        return f"repo-identity:{self.repo_slug}->{self.agent_id}"
