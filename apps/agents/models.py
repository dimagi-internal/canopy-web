"""First-class AI-agent workspace.

An `Agent` is a first-class entity (e.g. "Echo," a marketing agent) — distinct
from a code Project. Where a Project's work shows up as static markdown
shareouts, an Agent's periodic sync is a **Google Doc** (`AgentSync.doc_url`),
because an agent's sync spans both code/skill improvement AND the work it
delivered, and is richer than a feed card. canopy-web stores the metadata, summary, and
self-grades for the feed; the body lives in the doc.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from pgvector.django import HalfVectorField


class Agent(models.Model):
    """A first-class AI agent that publishes its work into canopy-web."""

    slug = models.SlugField(unique=True, max_length=80)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    persona = models.TextField(blank=True, default="", help_text="Who the agent is / its remit.")
    email = models.EmailField(blank=True, default="", help_text="The agent's own mailbox, if any.")
    #: The Google account the agent signs in to Drive, Docs and gog with — apart from
    #: `email`, which may be a canopy-hosted address (`ada@dimagi.ai`) no Google account
    #: holds. Blank means "the same as `email`" when that is a Google mailbox
    #: (`google_identity`), so every agent that predates the split keeps working.
    google_account = models.EmailField(
        blank=True, default="",
        help_text="The agent's Google account (Drive, Docs, gog), when it is not its mailbox.")
    avatar_url = models.URLField(blank=True, default="", max_length=500)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agents",
        help_text="The human who operates the agent.",
    )
    #: The DECLARED INTERFACE — what callers (anyone not the owner or an admin)
    #: may ask this agent for. LIVE STATE held here, not in the agent's repo:
    #: edited on the agent's page (or `canopy agent interface set`), validated by
    #: `apps.agents.interface.parse`.
    #: Empty means "not published", and every turn runs in the full profile,
    #: exactly as before interfaces existed. See `apps/agents/interface.py`.
    interface = models.JSONField(default=dict, blank=True)
    #: The interface as its editor wrote it (YAML, comments and all). `interface`
    #: is its parsed, validated form — the one everything enforces.
    interface_source = models.TextField(blank=True, default="")
    interface_published_at = models.DateTimeField(null=True, blank=True)
    interface_published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    #: How many projects this agent has ever been given, so `P<N>` is never
    #: reused. Deriving the next id from what EXISTS would hand "P2" back out
    #: after P2 was deleted, and an old link (a task's Links, a Drive folder
    #: name, an email) would then point at different work.
    project_seq = models.PositiveIntegerField(default=0)
    #: The same counter for tasks, so an ask raised by machinery can be numbered
    #: without the caller first listing every task to work out the next T<N>.
    task_seq = models.PositiveIntegerField(default=0)
    #: The agent's OWN canopy login — not `owner`, which is the human operating
    #: it. An agent authenticates to canopy's own MCP with its own PAT
    #: (`secret_refs: canopy-pat`), so the caller behind a tool call is this
    #: user, and every per-caller query resolves against it.
    #:
    #: Explicit rather than matched on `email`: that field is blank by default
    #: and unique on neither side, so a blank-to-blank match would authorize
    #: every agent against every user without one. That is the
    #: `workspace_id IS NULL means allow` bug in a different costume, and this
    #: repo has paid for it once already (agents/0013).
    #:
    #: Nullable because an agent need not have a canopy login — but a NULL must
    #: never match anything. Every predicate over this column filters
    #: `user__isnull=False` or compares against a concrete user id.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agent_identity",
        help_text="The agent's own canopy login, used when it calls canopy as itself.",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        related_name="agents",
        help_text="The tenant that owns this agent. NOT NULL: an agent always "
        "has exactly one tenant, so no tenancy predicate anywhere may treat "
        "NULL as 'allow'.",
    )
    # NOT NULL is a SECURITY invariant, not tidiness. This column was nullable
    # "for migration safety" from 0006 until 0013, long after 0007's backfill
    # discharged that need — and in that window six separate tenancy predicates
    # independently grew a `workspace_id IS NULL` leg that reads as ALLOW
    # (agents/api.py, harness/api.py x2, harness/services.py x2,
    # harness/schedule_services.py, realtime/snapshot.py, agent_runs/api.py).
    # Four were fixed one site at a time (PRs #378, #421, #423) before it was
    # clear that the recurrence — not any single site — was the defect: a
    # nullable tenant FK invites `if agent.workspace_id and <check>`, which
    # short-circuits to "ungated" on exactly the row that has no tenant.
    # Migration 0013 backfills + constrains so the fail-open state is
    # unrepresentable rather than merely unpopulated. Do not re-nullable this.
    # --- Runtime registry (agent-runtime-registry design) ----------------
    # canopy-web is the entry point a runner asks "how do I run agent X?": it holds
    # the POINTER to the agent's repo (whose runtime.yaml is the declarative WHAT),
    # the secret REFERENCES (names, never values — resolved from 1Password on a
    # laptop / Secrets Manager on a cloud box), and the engine preference. The
    # declarative spec itself lives in the repo; secret values live in the store.
    EMDASH, CLOUD_P, ANY_ENGINE = "emdash", "cloud_p", "any"
    ENGINE_CHOICES = [(EMDASH, "emdash"), (CLOUD_P, "cloud-p (claude -p)"), (ANY_ENGINE, "any")]
    repo_url = models.CharField(
        max_length=300, blank=True, default="", help_text="Agent's git repo (holds runtime.yaml).",
    )
    repo_ref = models.CharField(max_length=120, blank=True, default="main")
    runtime_engine = models.CharField(max_length=10, choices=ENGINE_CHOICES, default=ANY_ENGINE)
    runtime_secrets = models.JSONField(
        default=list, blank=True,
        help_text="Secret-reference NAMES this agent needs (never values), e.g. "
        '["canopy-pat","echo-gog"]. The reconciler resolves each from the env store.',
    )
    # Ordered runner-KIND preference, e.g. ["cloud", "emdash"]. As of the
    # directed-runner-routing cascade (spec 2026-07-24), agent-turn claim
    # eligibility is decided by RunnerAssignment rank instead — this field is no
    # longer read by harness.services.claim_next_turn. Retained only for the
    # deprecated PATCH /api/agents/{slug}/runner-preference/ endpoint and
    # AgentIn during the deprecation window.
    runner_preference = models.JSONField(default=list, blank=True)
    # WHERE each declared secret's value comes from, mirrored from the agent
    # repo's runtime.yaml: {"<ref-name>": {"op": "op://..."} | {"value": "..."}}.
    #
    # runtime_secrets says an agent NEEDS `gog-token`; this says that value lives
    # at op://Agent-Ace/gog-token/credential. Without it an importer can only
    # GUESS a convention, and a guess here is not safe: deriving
    # op://Agent-Ace/<name>/credential for ACE's gog-oauth-client resolves
    # cleanly to a DIFFERENT OAuth app in a different GCP project than the one it
    # uses. A convention that silently returns the wrong credential is worse than
    # one that fails (ace#2060).
    runtime_sources = models.JSONField(
        default=dict, blank=True,
        help_text='Per-ref value source from runtime.yaml: {"name": {"op": "op://..."}} '
                  'or {"name": {"value": "literal"}}. Never holds a secret VALUE for an '
                  "op-backed ref — only where to find it.",
    )
    # The agent's own 1Password vault + a service-account token scoped to it.
    # Per-agent rather than fleet-wide on purpose: a single key that can read
    # every vault makes canopy-web worth attacking for all of them at once,
    # where this bounds a compromise to one agent (Jonathan, 2026-09-06).
    op_vault = models.CharField(
        max_length=200, blank=True, default="",
        help_text="1Password vault holding this agent's secrets, e.g. Agent-Ace.",
    )
    op_sa_token_enc = models.TextField(blank=True, default="")
    # Which backend `canopy cred` resolves this agent's secrets from (canopy#850,
    # design revision 2026-10-09). Both are first-class: canopy-web already
    # custodies the agent's 1Password service-account key, so keeping values off
    # canopy-web buys no real security. `1password` = the runner/human resolves
    # through `op` with that key; `canopy-web` = values come from AgentCredential
    # via GET /credentials/resolve. Set only by the agent's owner or an admin
    # (PATCH /credential-source) — never by the repo's self-publish upsert.
    ONEPASSWORD, CANOPY_WEB = "1password", "canopy-web"
    CREDENTIAL_SOURCE_CHOICES = [(ONEPASSWORD, "1Password (op, via the agent's vault key)"),
                                 (CANOPY_WEB, "canopy-web (AgentCredential values)")]
    credential_source = models.CharField(
        max_length=16, choices=CREDENTIAL_SOURCE_CHOICES, default=ONEPASSWORD,
    )
    # Canopy Vault device binding (canopy-web#1538 Phase 1a). When on AND the
    # source is `canopy-web`, `/credentials/resolve` also demands a DPoP proof
    # signed by an ACTIVE RunnerDeviceKey on a runner this agent routes to (or
    # one its owner owns) — a PAT alone no longer reads the values. Owner/admin
    # only (PATCH /device-binding); absent from AgentIn like credential_source.
    require_device_binding = models.BooleanField(default=False)
    # Canopy Vault Phase 1b (canopy-web#1549): rotate the agent's own
    # `canopy-pat` every `pat_rotation_days` (apps/agents/pat_rotation.py).
    # Owner/admin opt-in (PATCH /pat-rotation); `pat_rotation_enabled_by` is the
    # person whose standing the background job acts on (the impersonation guard
    # in `mint_agent_canopy_token` is re-checked against them every rotation).
    pat_rotation_enabled = models.BooleanField(default=False)
    pat_rotation_days = models.PositiveSmallIntegerField(default=30)
    pat_rotation_enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    pat_rotated_at = models.DateTimeField(null=True, blank=True)
    # Canopy Vault Phase 2 (canopy-web#1550): zero-knowledge. When on, every
    # AgentCredential holds `zk1:` ciphertext under a per-agent vault key (VK)
    # canopy-web never sees; VK reaches each principal as an AgentVaultWrap.
    # `zk_kv` is the current vault-key version (0 = never enabled). Formats:
    # docs/vault/zk-v1/zk_v1_reference.py. Owner/admin only (apps/agents/zk.py).
    zk_enabled = models.BooleanField(default=False)
    zk_kv = models.PositiveIntegerField(default=0)

    # Runtime autonomy posture, read by the fleet-canonical turn procedure at
    # preflight (canopy agent-core/turn.md § Turn mode). Operational STATE, so
    # it lives here — on the board the human operates from — not as a committed
    # field in the agent's repo. Flipped only by a human (the /agents/<slug>
    # toggle or PATCH /turn-mode); deliberately absent from AgentIn so a repo's
    # self-publish upsert can never change its own autonomy.
    MANUAL, AUTO = "manual", "auto"
    TURN_MODE_CHOICES = [(MANUAL, "manual (outbound waits for human approval)"),
                         (AUTO, "auto (self-review-and-send, audit on the board)")]
    turn_mode = models.CharField(max_length=8, choices=TURN_MODE_CHOICES, default=MANUAL)
    # Reachable from Slack (apps/slack). Off by default and owner-only to flip:
    # it opens the agent to a new channel of people, which is a deliberate act.
    slack_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def google_identity(self) -> str:
        """The Google account this agent signs in to Google with, or "".

        `google_account` when set; else `email` when that is a Google mailbox — not a
        canopy-hosted address, which no Google account holds; else nothing."""
        if self.google_account:
            return self.google_account.strip().lower()
        if not self.email:
            return ""
        from . import mailbox

        return self.email.strip().lower() if mailbox.describe(self)["kind"] == "google" else ""

    def is_admin(self, user) -> bool:
        """The agent's owner, a WORKSPACE owner, or an explicit admin who is
        still a member of the workspace.

        Workspace owners are admins implicitly, not by a seeded row: one can
        already make themselves any agent's owner (`transfer_owner`), so
        excluding them would be theatre, and a row seeded once would miss every
        owner added later. The explicit grant is for everyone else — above all
        instead of `editor`, which an auto-approved access request can hand out.

        Fails closed: an anonymous user, an agent with no workspace, or anyone
        who is not a CURRENT member — an explicit grant's holder, and the owner
        too. The owner leg used to answer before membership was asked, so an
        owner removed from the workspace kept the whole agent: its credentials,
        its interface, the power to transfer it. Being the owner is a role on
        an agent inside a tenant, and outside the tenant there is no role.
        """
        if not getattr(user, "is_authenticated", False) or not self.workspace_id:
            return False
        from apps.workspaces import services as wsvc

        role = wsvc.member_role(user, self.workspace_id)
        if role is None:
            return False
        if self.owner_id is not None and self.owner_id == user.pk:
            return True
        # A workspace OWNER holds every agent's keys. A workspace ADMIN does
        # not: running the workspace (logs, members, integrations) is not
        # holding its agents' credentials, which is what this answers.
        from apps.workspaces import permissions as perms

        if perms.role_allows(role, perms.OWN):
            return True
        return self.admin_grants.filter(user=user).exists()

    def admin_user_ids(self) -> set[int]:
        """Every user `is_admin` answers yes for, in a fixed number of queries: the
        workspace's effective members (inherited owners included) who are its owner, a
        workspace owner, or hold an explicit grant. For listing — asking `is_admin` of
        each candidate costs queries per person."""
        if not self.workspace_id:
            return set()
        from apps.workspaces import permissions as perms
        from apps.workspaces import services as wsvc

        granted = set(self.admin_grants.values_list("user_id", flat=True))
        return {m.user_id for m in wsvc.effective_memberships(self.workspace)
                if m.user_id == self.owner_id or m.user_id in granted
                or perms.role_allows(m.role, perms.OWN)}

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"agent:{self.slug}"


class AgentAdmin(models.Model):
    """A person the agent's owner has trusted with the WHOLE agent.

    Phase 3 of the who-is-asking spec (§3). An agent has two relationships: its
    owner and admins reach its full working session and may reshape it and hold
    its keys; everyone else is a caller. Before this, "may change this agent"
    was a WORKSPACE role, and the one that mattered — `editor` — is handed to
    anyone from an allowed domain who clicks "join". Being in a workspace and
    being trusted with an agent's credentials are different things, so this is
    an explicit, per-agent grant with a name on it.

    The owner is always an admin (`Agent.is_admin`) without a row here. A grant
    counts only while its holder is still a member of the agent's workspace: a
    row that outlives the membership is inert, never a way back in.
    """

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="admin_grants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="agent_admin_grants")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+",
                                   help_text="Null for a grant seeded by migration.")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "user"],
                                               name="uniq_agent_admin")]
        ordering = ["granted_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user_id} admin of {self.agent_id}"


class AgentBootstrapReport(models.Model):
    """What a BOX actually managed to materialize for an agent, as the box saw it.

    The gap this closes, in one sentence: `agent_credential_status` answers
    "is the credential stored here", and the question that actually matters is
    "can the box USE it" — and those came apart badly on 2026-09-07.

    canopy-web held a valid, freshly-minted gog-token for ACE. The status screen
    would have shown `gog-token: set` with a green tick. On the box, the OAuth
    client id+secret that token is useless without had failed to materialize, so
    every gmail call returned `No auth for gmail ace@dimagi-ai.com`. Both facts
    were true at once, and only one of them was visible anywhere but journald.

    That is why this is reported BY the box rather than inferred from what is
    stored. A control plane that reports its own intentions back to itself
    cannot see this class of failure at all — the same reason the credentials
    screen is not proof a mailbox is alive, and a pinned check is.

    One row per (agent, runner): the answer is per-box, because two boxes
    running the same agent can differ, and "which box" is the first thing you
    need when one of them is broken.
    """

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE,
                              related_name="bootstrap_reports")
    #: The box's own name for itself ($RUNNER_NAME on the runner). A string
    #: rather than an FK because that is what bootstrap_agents.sh actually
    #: knows about itself — runner.env carries RUNNER_NAME and no id — and
    #: inventing a lookup would add a failure mode to a diagnostic.
    runner_name = models.CharField(max_length=200)
    #: Did the OAuth CLIENT credential land (credentials-<client>.json)? The
    #: half that failed on 2026-09-07 and the half nothing was watching.
    client_creds_ok = models.BooleanField(default=False)
    #: Can the mailbox actually authenticate — the box ran the call, not a
    #: guess from what is configured.
    mailbox_ok = models.BooleanField(default=False)
    #: Which gog client the box ended up using. A browser mint binds the token
    #: to `canopy-web`, a laptop `gog login` to `canopy`; either serves a fleet
    #: agent's turns (2026-10-04), so this says which door the token came through.
    gog_client = models.CharField(max_length=120, blank=True, default="")
    #: The client the agent's TURNS will actually present — what
    #: `config/agent.json.gog_client` declares (`canopy` fleet-wide), or — when a
    #: token under the OTHER fleet client serves it — the client turns will
    #: actually resolve to (canopy `reconcile_client`). Distinct from `gog_client`
    #: above: that one is whichever client the box's token was minted under.
    turn_client = models.CharField(max_length=120, blank=True, default="")
    #: Did the agent's secrets materialize (`op inject` of its `.env.tpl`)?
    #: Null = the box did not say. False means the box kept whatever `.env` it
    #: already had, so the agent still runs — on secrets that may be stale, and
    #: without any key added to the template since. Observed on cloud-ec2-1
    #: (2026-09-22): four agents failing this for two weeks, visible only in
    #: journald.
    env_ok = models.BooleanField(null=True, blank=True)
    #: Can the mailbox authenticate under `turn_client` — the client the CONSUMER
    #: uses, not the one the verifier picked. Null means the box did not check
    #: (an older box, or gog absent), which must not read as False.
    #:
    #: This exists because `mailbox_ok` was TRUE for a full day while every ACE
    #: email turn blocked at preflight (2026-09-08). Both halves were internally
    #: consistent and they disagreed: bootstrap probes the client whose token
    #: AUTHENTICATES (deliberately — see the GOG_CLIENT note in
    #: bootstrap_agents.sh), while `/ace:turn` presents the client its config
    #: DECLARES. ACE's token was minted in a browser and so bound to
    #: `canopy-web`; its turns ask for `canopy`; the box had no such token, and
    #: the readiness report said the mailbox was live the whole time.
    #:
    #: A check that passes under a client the consumer never uses is not a check.
    #: (Since 2026-10-04 turns accept EITHER fleet client, so a `canopy-web` token
    #: for a `canopy` declaration is turn-ready; this now fails only when no fleet
    #: client can read the mailbox, or the token is under a non-fleet client.)
    turn_ready = models.BooleanField(null=True, blank=True, default=None)
    #: One line, from the box. The reason, when there is one.
    detail = models.TextField(blank=True, default="")
    reported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["agent_id", "runner_name"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "runner_name"],
                                    name="uniq_bootstrap_report_per_agent_runner"),
        ]

    def __str__(self):
        return f"bootstrap:{self.agent_id}@{self.runner_name}"


class ZeroKnowledgeValue(ValueError):
    """A custodial path asked for the plaintext of a zero-knowledge value."""


class AgentCredential(models.Model):
    """One named secret for one agent, encrypted at rest.

    Spec: docs/superpowers/specs/2026-09-05-agent-credentials-design.md

    canopy-web is the agent secret store so that standing up an agent does not
    require a 1Password vault someone must be granted. 1Password remains an
    import source and a per-secret FALLBACK, which is what makes the migration
    reversible: an agent with nothing here keeps working.

    Write-only over the API. The value is readable by exactly one caller — a
    runner that could actually run this agent — and never by a browser, which is
    what makes "write-only" a property of the system rather than of the UI.

    NAMED SLOTS, not fixed columns: RunnerCredential has five columns because a
    runner has five credentials; ACE declares 45 refs and echo 12.

    Deliberately carries NO `optional`/`env`/`path` metadata. That is the
    DECLARATION's job, and it lives in the agent's runtime.yaml (reaching
    canopy-web as Agent.runtime_secrets). Storing shape here too is the
    two-writers mistake that produced the gog_client drift — three copies of one
    fact, two of them wrong.
    """

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="credentials")
    #: Matches a `secrets[].name` in the agent's runtime.yaml.
    name = models.CharField(max_length=120)
    #: Fernet ciphertext (apps/common/encryption.py). Never plaintext. Empty
    #: for a zero-knowledge value (`value_zk`).
    value_enc = models.TextField(blank=True, default="")
    #: Zero-knowledge (canopy-web#1550): `zk1:` AES-GCM ciphertext under the
    #: agent's vault key at version `kv`. canopy-web cannot decrypt it, so
    #: `plaintext()` refuses it.
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
            )
        ]

    def __str__(self) -> str:
        return f"{self.agent.slug}:{self.name}"

    # ---- the one codec for `value_enc` (canopy-web#1538) ---------------------
    # Fernet by default; `kms1:` envelope when settings.VAULT_KMS_KEY_ID is set
    # (apps/common/kms_envelope.py). Every reader goes through `plaintext()` so
    # both formats are always readable, whichever is being written today.

    @staticmethod
    def kms_context(agent, name: str) -> dict[str, str]:
        return {"workspace": str(agent.workspace_id), "agent": str(agent.pk), "name": name}

    @classmethod
    def seal(cls, agent, name: str, plaintext: str) -> str:
        from apps.common import kms_envelope
        from apps.common.encryption import encrypt_secret

        if kms_envelope.enabled():
            return kms_envelope.encrypt(plaintext, context=cls.kms_context(agent, name))
        return encrypt_secret(plaintext)

    @property
    def is_zk(self) -> bool:
        return bool(self.value_zk)

    def plaintext(self) -> str:
        from apps.common import kms_envelope
        from apps.common.encryption import decrypt_secret

        if self.is_zk:
            # canopy-web holds no key for this (canopy-web#1550); a caller that
            # reaches here has a custodial path a zero-knowledge agent must not.
            raise ZeroKnowledgeValue(f"{self.name} is a zero-knowledge value; canopy-web cannot read it")
        if kms_envelope.is_envelope(self.value_enc):
            return kms_envelope.decrypt(
                self.value_enc, expect=self.kms_context(self.agent, self.name))
        return decrypt_secret(self.value_enc)


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
    #: `runner` (a box that runs the agent) or `admin` (a human resolving by hand).
    via = models.CharField(max_length=16, blank=True, default="")
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
    told (Canopy Vault Phase 1b, canopy-web#1549; `apps/agents/vault_alerts.py`).

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
    §7; spec 2026-10-19-give-an-agent-a-secret-design.md §4).

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


class AgentSecretRequest(models.Model):
    """An agent asking for one of its declared secrets (spec
    2026-10-19-give-an-agent-a-secret-design.md). A request carries NO value,
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


class ZkGrantNotice(models.Model):
    """A wrap target the agent's vault key has not reached yet, and whether the
    people who hold that key were told (canopy-web#1550; `apps/agents/zk_notify.py`).

    One row per (agent, kind, target, kv): seen first at `first_seen_at`, told
    at most once (`notified_at`), closed (`cleared_at`) once the target is
    wrapped or stops qualifying. A target that needs a grant again at a later kv
    gets a new row."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="zk_grant_notices")
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
        ]

    def __str__(self) -> str:
        return f"{self.agent_id}:kv{self.kv}:{self.kind}:{self.target}"


class AgentPatRotation(models.Model):
    """One automatic (or on-demand) rotation of an agent's `canopy-pat`
    (Canopy Vault Phase 1b, canopy-web#1549; `apps/agents/pat_rotation.py`).

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


class AgentDelegation(models.Model):
    """A PERSON lending their own identity on some service to ONE agent.

    The third kind of credential, beside the agent's own (its 1Password vault —
    things that ARE the agent: its mailbox, an account of its own) and the
    tenant's (the workspace's shared vault). `docs/superpowers/specs/
    2026-09-05-agent-credentials-design.md` named this case and left it out of
    scope — "`chrome-sales` acts on behalf of the dispatching human, not the
    agent … an agent vault is the wrong home" — and GitHub is the same shape: an
    agent's pull request should be opened AS its owner, with the owner's rights.

    Why a row of its own and not a vault item (Jonathan, 2026-09-24):

    - **It belongs to the person, not the agent.** A delegation is USED only
      while its person owns the agent (`services.delegation_for`), so handing an
      agent to someone else stops the old owner's identity travelling with it,
      and the new owner is asked for their own. A vault item would have gone
      with the agent.
    - **It is scoped per agent.** For GitHub the owner pastes a fine-grained
      token whose repository selection IS what this agent may touch — the only
      per-repository scoping GitHub enforces on a credential that acts as a
      person. Whether two agents hold the same token is the owner's business;
      canopy stores one row each and never compares them.
    - **canopy-web is its only home.** The "canopy-web holds only what it
      mints" rule is about agent secrets that already live in 1Password. A
      delegation has nowhere else to live, so it is held here — encrypted,
      write-only, never returned to a browser — and handed to a runner one TURN
      at a time (`POST /api/harness/runners/{id}/turns/{id}/github-token`).

    `meta` is what canopy last learned about the secret, never the secret:
    for GitHub `{login, user_id, name, expires_at, checks: [{repo, ok,
    detail}], checked_at}`.
    """

    GITHUB = "github"
    #: Salesforce is lent by an AGENT, not a person: chrome-sales acts in Salesforce
    #: as one delegated identity (Eva's SF user, since 2026-10-07), and the other
    #: agents borrow it. The row holds no secret (`secret_enc` is ""): it names the
    #: `lender`, and the credential is resolved from the lender's own
    #: `AgentCredential` named `salesforce` at use time — so a re-mint by the lender
    #: reaches every borrower, and deleting one row stops one borrower. A copy per
    #: borrower would be N secrets to rotate (canopy-web#1291).
    SALESFORCE = "salesforce"
    SERVICE_CHOICES = [(GITHUB, "GitHub"), (SALESFORCE, "Salesforce")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_delegations",
    )
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="delegations")
    service = models.CharField(max_length=32, choices=SERVICE_CHOICES)
    secret_enc = models.TextField(blank=True, default="")
    #: The agent whose OWN credential is lent (Salesforce). Null for a person's
    #: own secret held in `secret_enc` (GitHub).
    lender = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, null=True, blank=True,
        related_name="lent_delegations",
    )
    meta = models.JSONField(default=dict, blank=True)
    #: From the service when it says (GitHub's token-expiration header); null
    #: for a token that never expires. A column, not just `meta`, so "what
    #: expires soon" is a query.
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "agent", "service"],
                                    name="uniq_agent_delegation"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"delegation:{self.user_id}->{self.agent_id}:{self.service}"


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


class AgentSync(models.Model):
    """A periodic manager sync (a status report) — a Google Doc covering code/skill
    improvement AND the work delivered. Body lives in `doc_url`; canopy-web keeps the summary +
    self-grades for the feed. Idempotent per (agent, period_start, period_end,
    source): re-posting the same window from the same source replaces it."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="syncs")
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default="")
    doc_url = models.URLField(max_length=500, help_text="The Google Doc holding the full sync.")
    # Structured self-grades, e.g. {"work": "C+", "skills": "B-"}.
    self_grades = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end", "-created_at"]
        indexes = [models.Index(fields=["agent", "-period_end"])]

    def __str__(self):
        return f"sync:{self.agent.slug}:{self.period_start}..{self.period_end}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentSkill(models.Model):
    """An entry in the agent's skill catalog: what the skill does, a link to its
    definition (SKILL.md), and the latest improvement note. The catalog is
    replaced wholesale on each publish (PUT) so it always mirrors the repo."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="skills")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="", help_text="Link to the SKILL.md.")
    improvement_note = models.TextField(blank=True, default="")
    # Which skills are human entry points. The catalog mirrors the repo, and most
    # of what a mature agent publishes is not launchable: a pre-send discipline
    # another skill invokes, a superseded skill, a reference doc, a one-time
    # per-machine setup. Rendering the whole catalog as phone buttons puts
    # `/echo:setup` one thumb away. Defaults False so an agent that has not
    # adopted the field publishes nothing launchable — fail closed.
    launchable = models.BooleanField(
        default=False, help_text="Offer this skill as a command on the phone composer."
    )
    args_hint = models.CharField(
        max_length=120, blank=True, default="",
        help_text='What the command takes, e.g. "topic (optional)". Renders as the input placeholder.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_skill"),
        ]

    def __str__(self):
        return f"skill:{self.agent.slug}:{self.name}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentProject(models.Model):
    """A real project of this agent's — the thing its `Projects/<name>` Drive
    folder holds.

    **Why canopy holds a record and Drive still holds the files.** The layout in
    `agent-core/deliverables.md` is per agent and non-negotiable: one Drive root
    per agent, `Projects/<name>/` beneath it, deliverables inside. That is a good
    home for FILES and a poor one for state — a folder cannot say who the work is
    waiting on, whether it is finished, or which tasks belong to it, and nothing
    can list "everything in flight" across agents by reading Drive. So the folder
    stays exactly where it is and this row points AT it.

    **Two scopes.** An AGENT project (the default) is a piece of that agent's
    work with an end; two agents on one initiative have a project each, as they
    have a Drive folder each. A WORKSPACE project is a standing resource many
    agents share — a funder knowledge base, say — that outlives every task
    citing it. It is owned by the workspace (`owner_workspace`), addressed by a
    `handle` unique there (`dimagi/givewell-kb`), and `agent` is its STEWARD:
    the one agent that keeps it current, whose Drive folder holds its files.
    The steward can change without the project's address changing, any agent
    in the workspace can file tasks into it, the steward may belong to the
    workspace or one above it (an org's agent curating a division's resource:
    `connect/givewell-kb`, stewarded by dimagi's Eva), and an agent cannot be moved or
    deleted out from under a workspace project it stewards
    (`services.set_steward`, `move.py`, `api.delete_agent`).

    This is canopy's one project system (the pre-agentic workbench registry of
    repos was retired 2026-10-06). A project is a piece of work with an end —
    "UNGA 2026 conference planning" — and names a repo it touches only as a
    slug (`repo_slug`); there is no repo registry to point at.
    """

    #: `proposed` — an agent proposed it (from a huddle, a chat, its own turn) and
    #: nobody has started or declined it yet; its decision card ("Start this
    #: project?") is how a person does either (`apps/agents/proposals.py`).
    PROPOSED, ACTIVE, DONE, ARCHIVED = "proposed", "active", "done", "archived"
    STATUS_CHOICES = [(PROPOSED, "Proposed"), (ACTIVE, "Active"), (DONE, "Done"), (ARCHIVED, "Archived")]

    AGENT_SCOPE, WORKSPACE_SCOPE = "agent", "workspace"
    SCOPE_CHOICES = [(AGENT_SCOPE, "Agent project"), (WORKSPACE_SCOPE, "Workspace project")]
    HANDLE_RE = r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$"

    #: The owning agent of an agent project; the STEWARD of a workspace project.
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="projects")
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default=AGENT_SCOPE)
    #: Set exactly when `scope` is workspace: the workspace that owns it. The
    #: steward's workspace or one below it (`services.may_hold_workspace_project`).
    owner_workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True,
                                        on_delete=models.CASCADE, related_name="workspace_projects")
    #: A workspace project's address, unique in its workspace ("givewell-kb").
    #: Blank for agent projects, which are addressed `<agent>/<ext_id>`.
    handle = models.CharField(max_length=60, blank=True, default="")
    #: What a workspace project SERVES as its own MCP server at `mcp_path`
    #: (`apps/mcp/projects.py`): "" nothing, "kb" a knowledge base its steward
    #: publishes (`kb_*` below; tools from the KB's own manifest, `canopy_kb.tools`).
    MCP_NONE, MCP_KB = "", "kb"
    MCP_KIND_CHOICES = [(MCP_NONE, "None"), (MCP_KB, "Knowledge base")]
    mcp_kind = models.CharField(max_length=16, choices=MCP_KIND_CHOICES, blank=True, default="")
    #: The published kb.sqlite.gz: its object key in the KB bucket, content hash
    #: (what a serving instance caches by) and size. Set by `kb_store.publish`.
    kb_key = models.CharField(max_length=300, blank=True, default="")
    kb_sha256 = models.CharField(max_length=64, blank=True, default="")
    kb_bytes = models.BigIntegerField(null=True, blank=True)
    kb_published_at = models.DateTimeField(null=True, blank=True)
    #: Stable id the agent addresses it by, unique per agent — the same shape as
    #: `AgentTask.ext_id` ("P1", "P2"), so the CLI can name a project without
    #: guessing at database ids.
    ext_id = models.CharField(max_length=64, help_text="Stable id, unique per agent (P1, P2 …).")
    name = models.CharField(max_length=200, help_text="What the project is called — and its Drive folder's name.")
    outcome = models.TextField(blank=True, default="", help_text="What done looks like.")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=ACTIVE)

    #: The human who owns the outcome. A real person where canopy knows them, so
    #: "waiting on you" can reach someone; free text otherwise, because half the
    #: stakeholders in the fleet's boards today are names canopy has never seen.
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="agent_projects",
    )
    owner_note = models.CharField(max_length=200, blank=True, default="")

    #: The agent's own `Projects/<name>` folder. The id is what `gog drive` needs;
    #: the url is what a human clicks. Blank until the agent files something.
    drive_folder_id = models.CharField(max_length=128, blank=True, default="")
    drive_folder_url = models.URLField(max_length=500, blank=True, default="")

    #: The repo this work touches, as a slug, if any — the same bare repo name
    #: `Turn.project` carries; there is no repo registry to FK.
    repo_slug = models.CharField(max_length=100, blank=True, default="")

    notes = models.TextField(blank=True, default="")
    links = models.JSONField(default=list, blank=True)
    #: The page that proposed this project — a huddle, a thread — the same idea
    #: as `AgentTask.source_url`. Indexed: a huddle page lists the projects whose
    #: `source_url` is that page.
    source_url = models.URLField(max_length=500, blank=True, default="", db_index=True)
    #: When it was last proposed (or revised). A proposed project nobody starts or
    #: declines within `proposals.EXPIRES_AFTER` of this is archived, not taken up.
    proposed_at = models.DateTimeField(null=True, blank=True)
    #: The task this project grew out of — a check-in's proposed task that turned
    #: out to be a project's worth of work (`projects/propose/ from_task`). The
    #: task shows "spawned → this project"; this one shows "from <task> in <project>".
    spawned_from = models.ForeignKey("agents.AgentTask", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="spawned_projects")
    #: The PEOPLE this work involves (fleet brain v1.1, canopy#804) — not the
    #: owner (one human who owns the outcome) but everyone canopy has seen take
    #: part: the subject of a fact filed against the project, or the human whose
    #: conversation raised one of its tasks. Filled automatically
    #: (`apps/agents/participants.py`); see `ProjectParticipant`.
    participants = models.ManyToManyField(
        "contacts.Person", through="agents.ProjectParticipant", related_name="agent_projects",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "ext_id"], name="agent_project_ext_id_unique"),
            models.UniqueConstraint(fields=["owner_workspace", "handle"],
                                    condition=models.Q(scope="workspace"),
                                    name="workspace_project_handle_unique"),
            # owner + handle exactly when it is a workspace project, never half of one
            models.CheckConstraint(
                condition=(models.Q(scope="agent", owner_workspace__isnull=True, handle="")
                           | (models.Q(scope="workspace", owner_workspace__isnull=False)
                              & ~models.Q(handle=""))),
                name="project_scope_fields_agree"),
        ]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.ext_id} {self.name}"

    # Schemas here are plain pydantic (`StrictModel`), not ninja `Schema`, so a
    # `resolve_*` on the schema is ignored — the models carry these instead, the
    # way `AgentTask.agent_slug` already does.
    @property
    def agent_slug(self) -> str:
        return self.agent.slug

    @property
    def workspace(self) -> str:
        """The workspace this row lives in — the owner of a workspace project,
        else its agent's. A list pinned to a parent workspace also carries its
        children's rows, and the UI badges the ones from elsewhere."""
        return self.owner_workspace_id or self.agent.workspace_id

    @property
    def is_workspace_project(self) -> bool:
        return self.scope == self.WORKSPACE_SCOPE

    @property
    def steward_slug(self) -> str | None:
        """The agent that keeps a workspace project current (None for an agent project)."""
        return self.agent.slug if self.is_workspace_project else None

    @property
    def address(self) -> str:
        """How every caller names it: `<workspace>/<handle>`, else `<agent>/<ext_id>`."""
        if self.is_workspace_project:
            return f"{self.owner_workspace_id}/{self.handle}"
        return f"{self.agent.slug}/{self.ext_id}"

    @property
    def url(self) -> str:
        if self.is_workspace_project:
            return f"/w/{self.owner_workspace_id}/projects/{self.handle}"
        return self.board_url

    @property
    def board_url(self) -> str:
        """Its page on its agent's (steward's) board — in the AGENT's workspace,
        which for a division's project stewarded from the parent is not the
        project's (`connect/givewell-kb` → `/w/dimagi/agents/eva/projects/P12`)."""
        return f"/w/{self.agent.workspace_id}/agents/{self.agent.slug}/projects/{self.ext_id}"

    @property
    def mcp_path(self) -> str:
        """Where its own MCP server answers — a workspace project serving
        something, else "". Under `/api/mcp/` so the same sign-in (a personal
        token, or OAuth) reaches it: one server per project, so a reader adds just
        this one and not the whole canopy surface."""
        if self.is_workspace_project and self.mcp_kind and self.kb_key:
            return f"/api/mcp/projects/{self.owner_workspace_id}/{self.handle}/"
        return ""

    @property
    def owner_email(self) -> str | None:
        return self.owner_user.email if self.owner_user_id else None

    @property
    def spawned_from_ref(self) -> dict | None:
        """The task this project grew out of, as the page links it (None for most)."""
        if not self.spawned_from_id:
            return None
        t = self.spawned_from
        return {"agent": t.agent.slug, "ref": t.ext_id, "title": t.title, "status": t.status,
                "project_ref": t.project.ext_id if t.project_id else "",
                "project_name": t.project.name if t.project_id else "",
                # the project's own page: a workspace project's task may be another agent's
                "url": (t.project.url if t.project_id else f"/w/{t.agent.workspace_id}/agents/{t.agent.slug}/tasks")}

    @property
    def task_count(self) -> int:
        # The list route counts every project in one query and caches it here;
        # a single fetched project counts its own.
        cached = getattr(self, "_task_count", None)
        return self.tasks.count() if cached is None else cached

    @property
    def open_task_count(self) -> int:
        cached = getattr(self, "_open_task_count", None)
        if cached is not None:
            return cached
        return self.tasks.filter(status__in=AgentTask.LIVE_STATUSES).count()

    @property
    def waiting_task_count(self) -> int:
        """Live tasks somebody has to act on (`services.waiting_q`) — an open
        ask or a task parked on a person. Not per viewer: the Projects page
        asks "how much of this is stuck on people", not "on me"."""
        cached = getattr(self, "_waiting_task_count", None)
        if cached is not None:
            return cached
        from apps.agents.services import waiting_q

        return self.tasks.filter(waiting_q()).count()


class KbPassage(models.Model):
    """One passage of a workspace project's published knowledge base, as the
    semantic half of its `search` needs it: where it is, what its vector was made
    from (`sha256` of `canopy_kb.hybrid.embedding_text`) and the vector.

    The passage text stays in the KB file (`kb_store`); a search maps a nearest
    (item_id, ord) back to its row there. Rows are written only by
    `kb_vectors.sync`, which diffs the file by `sha256` and embeds only what is
    new or changed. A row whose `embedding_model` is not the current one counts
    as missing. On PostgreSQL an HNSW index (cosine) serves the nearest-neighbour
    query, filtered by project and type with pgvector's iterative scan
    (migration 0064); there is no vector search on SQLite."""

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_passages")
    item_id = models.CharField(max_length=300)
    ord = models.IntegerField()
    sha256 = models.CharField(max_length=64)
    item_type = models.CharField(max_length=64, blank=True, default="")
    #: As the KB has it: "YYYY-MM-DD", or "" when the item has no date.
    date = models.CharField(max_length=32, blank=True, default="")
    title = models.TextField(blank=True, default="")
    url = models.URLField(max_length=1000, blank=True, default="")
    embedding = HalfVectorField(dimensions=3072)
    embedding_model = models.CharField(max_length=100)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "item_id", "ord"], name="kb_passage_unique"),
        ]
        indexes = [models.Index(fields=["project", "item_type"], name="kb_passage_type")]

    def __str__(self):
        return f"kb:{self.project_id}:{self.item_id}#{self.ord}"


class ProjectParticipant(models.Model):
    """A person taking part in an agent's project (fleet brain v1.1, canopy#804).

    One row per (project, person). `source` says how canopy learned it, and the
    FIRST answer is kept: a later fact or turn about someone already linked
    changes nothing, so `source` reads "how did they first come in".

    Same tenancy as everything else about a person: a project belongs to one
    agent, which belongs to one workspace, so a participant row is only ever
    served to members of that workspace (`people.known_in` gates the person
    side of it).
    """

    FACT, TURN, MANUAL, SUMMARY, REVIEW = "fact", "turn", "manual", "summary", "review"
    BACKING, REPLY, CHECKIN = "backing", "reply", "checkin"
    SOURCE_CHOICES = [
        (FACT, "A fact about them was filed against the project"),
        (TURN, "A conversation with them raised one of its tasks"),
        (MANUAL, "Added by a person"),
        (SUMMARY, "An agent wrote their contributor page from their own words"),
        (REVIEW, "They commented in a feedback round on one of its storyboards"),
        (BACKING, "They backed (or passed on) the project"),
        (REPLY, "They replied on one of its tasks"),
        (CHECKIN, "They take part in one of its check-ins"),
    ]
    ROLE_MAX = 80

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE,
                                related_name="participant_rows")
    person = models.ForeignKey("contacts.Person", on_delete=models.CASCADE,
                               related_name="project_participations")
    #: Free text, e.g. "program lead", "LLO contact". Blank when canopy filled
    #: the row itself and nobody has said what part they play.
    role = models.CharField(max_length=ROLE_MAX, blank=True, default="")
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    #: Their CONTRIBUTOR PAGE (hal T69): what they are trying to achieve on this
    #: project, in their own words, with a link to where they said each thing,
    #: and which of the project's tasks are theirs. It is JUDGMENT — which of
    #: someone's 400 prompts say what they want — so an agent writes it
    #: (`contributors.write_summary`, `PUT …/contributors/{person}/summary/`) and
    #: the page only renders it. Per project because what someone wants from
    #: Supply says nothing about what they want from anything else.
    #:
    #: It is something an agent LEARNED about a person, so it follows the
    #: person's own switches: written only while they let agents learn about
    #: them (record) and this agent holds their grant; read by an agent only
    #: while they let agents use it (use). Empty dict = never written.
    summary = models.JSONField(default=dict, blank=True)
    summary_written_at = models.DateTimeField(null=True, blank=True)
    summary_by_agent = models.ForeignKey("agents.Agent", on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="contributor_summaries")

    class Meta:
        db_table = "agents_project_participant"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["project", "person"], name="one_participant_row_per_person"),
            models.CheckConstraint(condition=models.Q(source__in=["fact", "turn", "manual", "summary", "review",
                                                                  "backing", "reply", "checkin"]),
                                   name="project_participant_source_known_v5"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}<-{self.person_id} ({self.source})"


class ProjectBacking(models.Model):
    """Where one backer stands on a project — a person or an agent, never both.

    Backing is a SIGNAL, not a gate (decision 4(b), approved in
    `2026-10-16-work-group-huddles-design.md`): starting a proposed
    project is its decision card's, and no number of backers starts it. What
    this row buys is that "Echo backs · Eva backs with changes: … · Shayoni
    passes: …" is a row on the project, not a sentence in a huddle transcript.

    A row is on a project OR on a task (a check-in's proposed task, spec
    2026-10-17-project-check-ins-design.md §3), never both: one backing table.

    One row per (project, person) and one per (project, agent) — and the same per
    task: a backer changes
    their stance by rewriting their row. A later revision of the project marks
    every row `stale` — they backed the earlier version — and rewriting clears it.
    Written only through `apps/agents/proposals.back`.
    """

    BACKS, BACKS_WITH_CHANGES, PASSES = "backs", "backs_with_changes", "passes"
    STANCES = (BACKS, BACKS_WITH_CHANGES, PASSES)
    LINE_MAX = 280

    project = models.ForeignKey(AgentProject, null=True, blank=True, on_delete=models.CASCADE,
                                related_name="backings")
    task = models.ForeignKey("agents.AgentTask", null=True, blank=True, on_delete=models.CASCADE,
                             related_name="backings")
    person = models.ForeignKey("contacts.Person", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="project_backings")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="project_backings")
    stance = models.CharField(max_length=20, choices=[(s, s) for s in STANCES])
    #: One line, required unless the stance is plain `backs`: what to change, or why not.
    line = models.CharField(max_length=LINE_MAX, blank=True, default="")
    #: Where it was given: `web`, `mcp`, `slack`, `email`, or `huddle` (a co-sign
    #: `canopy huddle file` wrote from a member's round-3 answer).
    source = models.CharField(max_length=8, default="web")
    huddle_id = models.CharField(max_length=64, blank=True, default="")
    stale = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agents_project_backing"
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(person__isnull=False, agent__isnull=True)
                           | models.Q(person__isnull=True, agent__isnull=False)),
                name="project_backing_one_backer"),
            models.CheckConstraint(
                condition=(models.Q(project__isnull=False, task__isnull=True)
                           | models.Q(project__isnull=True, task__isnull=False)),
                name="project_backing_one_subject"),
            models.CheckConstraint(condition=models.Q(stance__in=["backs", "backs_with_changes", "passes"]),
                                   name="project_backing_stance_known"),
            models.UniqueConstraint(fields=["project", "person"], condition=models.Q(person__isnull=False),
                                    name="one_backing_per_person"),
            models.UniqueConstraint(fields=["project", "agent"], condition=models.Q(agent__isnull=False),
                                    name="one_backing_per_agent"),
            models.UniqueConstraint(fields=["task", "person"],
                                    condition=models.Q(person__isnull=False, task__isnull=False),
                                    name="one_task_backing_per_person"),
            models.UniqueConstraint(fields=["task", "agent"],
                                    condition=models.Q(agent__isnull=False, task__isnull=False),
                                    name="one_task_backing_per_agent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id or self.task_id}<-{self.person_id or self.agent_id}: {self.stance}"


class AgentTask(models.Model):
    """A task in the agent's tracker. Source of truth is a Google Sheet the
    agent maintains; Echo syncs rows here so canopy-web can render a board.
    `status` is normalized to one of the board columns. Synced wholesale per
    agent, keyed by `ext_id` (stable sheet-row id) so edits/removals
    propagate."""

    # Suggested = the agent proposed it; a human must validate (Linear "Triage").
    # No "To do" (the agent would just do it) and no "Blocked" column — "waiting
    # on a person" is expressed by `assigned` being a human, shown as an overlay.
    SUGGESTED, IN_PROGRESS, DONE, DECLINED = "suggested", "in_progress", "done", "declined"
    #: An OPEN task — the one definition every count of "open tasks" uses.
    LIVE_STATUSES = (SUGGESTED, IN_PROGRESS)
    STATUS_CHOICES = [
        (SUGGESTED, "Suggested"),
        (IN_PROGRESS, "In progress"),
        (DONE, "Done"),
        (DECLINED, "Declined"),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="tasks")
    ext_id = models.CharField(max_length=64, help_text="Stable id from the source sheet.")
    #: The project this task is part of. Optional: plenty of work is a one-off,
    #: and forcing a project on it would produce a project per task, which is
    #: what the Drive layout already warns against ("never dump flat in
    #: Projects/"). SET_NULL so closing a project does not delete its history.
    project = models.ForeignKey(
        "agents.AgentProject", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tasks",
    )
    #: The task this one is a step of. Subtasks, not subprojects (T72,
    #: Jonathan 2026-10-09): a product area is ONE project board, and a big
    #: piece of work inside it breaks down into child tasks rather than a new
    #: project per piece. Same agent and same project as the parent, no cycles,
    #: at most `MAX_TASK_DEPTH` levels — all enforced in `services.set_parent`,
    #: the one writer. SET_NULL: deleting a parent promotes its children to
    #: top-level tasks instead of deleting work.
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subtasks",
    )
    #: WHAT this task is (hal T82, Jonathan 2026-10-12). `work` — the agent's
    #: own work, and every task that says nothing. `idea` — a PERSON's want
    #: about the product, carrying their own words (at least one human-why
    #: quote, enforced in `services`); the work toward it is its subtasks.
    #: Explicit, never inferred: T69 read every task as an idea, and the agent's
    #: process work (a docs round-up, a screen review) counted as the product's
    #: shipped ideas.
    WORK, IDEA = "work", "idea"
    KIND_CHOICES = [(WORK, "Work"), (IDEA, "Idea")]
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=WORK)
    #: The task's own Drive folder, inside its project's — what
    #: `canopy gdoc publish --task` files under. The id is what `gog drive`
    #: needs; the url is what a human clicks. Blank until something is filed.
    drive_folder_id = models.CharField(max_length=128, blank=True, default="")
    drive_folder_url = models.URLField(max_length=500, blank=True, default="")
    # A task can mean "execute this run" — the run-lifecycle backing for the
    # board (spec §5). String FK ref ("agent_runs.AgentRun") so this framework
    # app doesn't import apps.agent_runs at module load (both are framework and
    # agent_runs imports apps.agents.models — a hard import here would cycle).
    # SET_NULL: deleting a run leaves the task, just unlinked.
    run = models.ForeignKey(
        "agent_runs.AgentRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        help_text="The run this task executes, if the task is run-backed.",
    )
    title = models.CharField(max_length=300, help_text="The outcome.")
    next_action = models.CharField(max_length=300, blank=True, default="", help_text="The single concrete next step (verb-first).")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=SUGGESTED)
    owner = models.CharField(max_length=120, blank=True, default="", help_text="Human stakeholder who owns the outcome — never the agent.")
    assigned = models.CharField(max_length=120, blank=True, default="", help_text="Who the next action waits on — the agent or a person.")
    confidence = models.CharField(max_length=10, blank=True, default="", help_text="high / low — how sure the agent is about a suggestion.")
    # The agent's self-assessment, captured WHEN the task is marked done — so a
    # manager sync reads the score from completion time instead of re-grading
    # weeks later. `score` is a short grade ("A-", "B+", "4/5"); `review` is the
    # one-line why. Blank until the task is completed.
    score = models.CharField(max_length=8, blank=True, default="", help_text="Self-grade at completion — e.g. A-, B+, 4/5.")
    review = models.TextField(blank=True, default="", help_text="One-line self-review captured when the task was completed.")
    # Context the agent stores when it suggests a task, so it doesn't re-derive
    # from email when a human says "go do it."
    rationale = models.TextField(blank=True, default="", help_text="Why the agent suggested this / why now.")
    source_url = models.URLField(max_length=500, blank=True, default="", help_text="Originating thread / report / link.")
    plan = models.TextField(blank=True, default="", help_text="Proposed first steps.")
    due = models.DateField(null=True, blank=True)
    links = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")
    #: Who the next step waits on, as a real person canopy can notify. The free
    #: text `assigned` stays beside it for counterparts canopy has never seen.
    waiting_on_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tasks_waiting_on",
    )

    # ---- the ask: what this task needs from a person ----------------------
    #: `review` ("should I do this?") or `question` ("I need an answer"); blank
    #: means the task asks nothing and is simply work in flight.
    ASK_NONE, ASK_REVIEW, ASK_QUESTION = "", "review", "question"
    ASK_CHOICES = [(ASK_NONE, "None"), (ASK_REVIEW, "Review"), (ASK_QUESTION, "Question")]
    ask_kind = models.CharField(max_length=10, choices=ASK_CHOICES, blank=True, default=ASK_NONE)
    #: The ask's own words, frozen when it was raised.
    ask_body = models.TextField(blank=True, default="")
    #: Set by the action that closed the ask; null while it is open. Who closed it
    #: and why is that action's row.
    ask_closed_at = models.DateTimeField(null=True, blank=True)

    #: Turn specs that run when the task is approved (or its question answered).
    on_approve = models.JSONField(default=list, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    #: One sitting (a fleet audit), so a batch can be rendered together.
    batch_key = models.CharField(max_length=64, blank=True, default="")
    #: Unique where set, so a producer re-posting a batch replays rather than
    #: duplicating. Null (not "") for a task nobody keyed, because unique
    #: columns treat NULLs as distinct and empty strings as equal.
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)

    #: Where the ask came from, when it was raised by machinery rather than
    #: typed by a person.
    origin = models.CharField(max_length=32, blank=True, default="")
    #: What a caller may post as `origin` (AgentTaskIn refuses anything else):
    #: blank, the postable Turn sources an ask was raised from (legacy spellings
    #: are normalized onto these — harness.schemas.normalize_origin), and the
    #: labels canopy's own CLI stamps on the tasks it files (`canopy agent task
    #: add` → task-tracker, `canopy agent dispatch` → dispatch, a huddle's
    #: outcomes → huddle). `canopy_scheduler` is server-set, never posted.
    POSTABLE_ORIGINS = ("", "api", "ace_web", "email", "slack",
                        "task-tracker", "dispatch", "huddle")
    origin_ref = models.JSONField(default=dict, blank=True)
    raised_by = models.ForeignKey(
        "harness.Turn", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="raised_tasks",
    )

    position = models.IntegerField(default=0, help_text="Order within a status column.")
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "position", "id"]
        constraints = [
            # Ignoring case: a task is looked up `ext_id__iexact`, so "t1" beside
            # "T1" would leave one of them unreachable.
            models.UniqueConstraint(Lower("ext_id"), "agent", name="uniq_agent_task_extid_ci"),
        ]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self):
        return f"task:{self.agent.slug}:{self.ext_id}:{self.status}"

    @property
    def waiting_on_email(self) -> str | None:
        return self.waiting_on_user.email if self.waiting_on_user_id else None

    @property
    def ask_is_open(self) -> bool:
        return bool(self.ask_kind) and self.ask_closed_at is None

    @property
    def project_ext_id(self) -> str | None:
        return self.project.ext_id if self.project_id else None

    @property
    def project_name(self) -> str | None:
        return self.project.name if self.project_id else None

    @property
    def project_address(self) -> str | None:
        """`<workspace>/<handle>` for a workspace project (whose ext_id is its
        steward's numbering, ambiguous on another agent's board), else
        `<agent>/<ext_id>`."""
        return self.project.address if self.project_id else None

    @property
    def parent_ext_id(self) -> str | None:
        return self.parent.ext_id if self.parent_id else None

    @property
    def agent_slug(self) -> str:
        return self.agent.slug

    @property
    def workspace(self) -> str:
        """The workspace this task lives in — its agent's (see `AgentProject.workspace`)."""
        return self.agent.workspace_id


    @property
    def human_why(self) -> list[dict]:
        """The people's own words behind this task, oldest first (`AgentTaskHumanWhy`).

        Reads the prefetch cache when the caller prefetched `human_whys`, so a list
        route costs one extra query rather than one per task."""
        return [w.as_dict() for w in self.human_whys.all()]


#: How deep subtasks nest: a task, its subtasks, and theirs. Deeper than that
#: is a project's worth of structure hiding inside one card — split it out.
MAX_TASK_DEPTH = 3


class AgentTaskHumanWhy(models.Model):
    """What a PERSON actually said that this task answers — quoted, attributed,
    linked — kept apart from the agent's `rationale` (T68).

    `rationale` is the agent's inference, and it never traced back to whoever
    wanted the work: "Canopy tasks do a decent job of explaining why the AI
    thinks a task matters, but they don't trace back to the person who
    originally wanted it" (the Supply brief, Part 3). A person's words weigh
    more than the agent's reading of them, so the board shows these first.

    **A row per quote, not a JSON list on the task**, for two reasons that are
    both about `who`:
      * `person` is a real foreign key, so the link survives (SET_NULL keeps the
        quote and its name/email if the Person goes) and cannot point at a row
        that never existed — a JSON id would.
      * "everything this person asked for, across every task" is the question
        contributor pages ask (T69). With rows it is one indexed query on
        `person`; with JSON it is a scan of every task's blob.
    The cost is one prefetch on the list routes. The list is still written
    wholesale (create, or a PATCH that sends `human_why`), like `links`.

    **Verbatim or nothing.** `quote` is the person's own words, never a
    paraphrase — an agent with no quote leaves the list empty.
    """

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="human_whys")
    position = models.PositiveSmallIntegerField(default=0)
    quote = models.TextField(help_text="The person's own words, verbatim.")
    #: The canopy Person who said it, when canopy knows them in the agent's
    #: workspace. Resolved from `who_email` (or given directly) at write time.
    person = models.ForeignKey("contacts.Person", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="task_quotes")
    #: Kept beside `person` so a quote from someone canopy has never seen still
    #: says who, and a deleted Person does not orphan the attribution.
    who_email = models.EmailField(blank=True, default="")
    who_name = models.CharField(max_length=200, blank=True, default="")
    #: Where they said it: the thread, the doc comment, the session.
    source_url = models.URLField(max_length=500, blank=True, default="")
    said_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.task_id}: {self.who_name or self.who_email}: {self.quote[:40]}"

    def as_dict(self) -> dict:
        return {
            "quote": self.quote,
            "who": {"person_id": self.person_id, "email": self.who_email, "name": self.who_name},
            "source_url": self.source_url,
            "said_at": self.said_at,
        }


class AgentTaskAction(models.Model):
    """Something a person did to a task. One row is both the task's history and,
    while `pending`, the agent's to-do: it drains pending rows on its next turn
    and marks each applied."""

    APPROVE, DECLINE, REPLY, NUDGE, DONE = "approve", "decline", "reply", "nudge", "done"
    ACTION_CHOICES = [(a, a) for a in (APPROVE, DECLINE, REPLY, NUDGE, DONE)]
    PENDING, APPLIED = "pending", "applied"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPLIED, "Applied")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="task_actions")
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="actions")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    comment = models.TextField(blank=True, default="")
    by = models.CharField(max_length=200, blank=True, default="")
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="task_actions")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=APPLIED)
    applied_at = models.DateTimeField(null=True, blank=True)
    result_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.task_id}:{self.action}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug

    @property
    def task_ext_id(self) -> str:
        return self.task.ext_id


class SkillHistorySync(models.Model):
    """The last pull of this agent's skill history from its repo.

    A CACHE of the repo, replaced wholesale on each sync — the same stance as
    `AgentSkill`. `groups` / `checks` / `present` are a HEAD-only snapshot read
    whole and never queried by field, so they are JSON rather than tables.

    `synced_with` is the GitHub login whose grant produced this history — the
    agent owner's, never the viewer's (see the spec's "Whose credential").
    """

    agent = models.OneToOneField(Agent, on_delete=models.CASCADE, related_name="skill_history_sync")
    # 64: a SHA-256 repository's object ids are 64 hex characters.
    head_sha = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    synced_with = models.CharField(max_length=100, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    # One of skill_history.CREDENTIAL_STATES — what the last attempt found.
    last_state = models.CharField(max_length=32, blank=True, default="")
    sync_started_at = models.DateTimeField(null=True, blank=True)
    # Set on every attempt, success or failure. `synced_at` moves only on
    # success, so without this a failing agent (no grant, repo not granted)
    # would refresh the owner's token and clone on every page load.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    groups = models.JSONField(default=list, blank=True)
    checks = models.JSONField(default=dict, blank=True)
    present = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"skill-history:{self.agent.slug}@{self.head_sha[:8]}"


class SkillHistoryCommit(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="skill_history_commits")
    sha = models.CharField(max_length=64)
    committed_at = models.DateTimeField()
    subject = models.TextField()
    body = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["committed_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "sha"], name="uniq_agent_skill_history_sha"),
        ]


class SkillRevision(models.Model):
    commit = models.ForeignKey(SkillHistoryCommit, on_delete=models.CASCADE, related_name="revisions")
    skill = models.CharField(max_length=120)
    lines_after = models.IntegerField()
    added = models.IntegerField()
    deleted = models.IntegerField()

    class Meta:
        indexes = [models.Index(fields=["skill"])]


class SenderTrust(models.Model):
    """A TEMPORARY exception: one address's mail ties to its member account
    without DMARC or a DKIM signature by the From: domain.

    The rule it bends is `harness.services._member_behind_email`: a member's
    email is linked to their account only when THIS message is aligned, because
    an unaligned From: is a header anyone can write. Some members' domains sign
    with a provider's key and publish no DMARC record (dimagi-associate.com,
    canopy-web#1265), so their own mail reaches the agent as a stranger's until
    the domain's admin fixes it. This is the stopgap while they do: an agent
    admin names the address, says why, and it EXPIRES — there is no permanent
    form, since a permanent one would be "accept a forgeable header" by default.

    Narrow on purpose:
      * one agent, one exact address — never a domain;
      * the message must still carry a passing DKIM signature (tier 2), so mail
        with no authentication at all is never accepted under it;
      * everything else `_member_behind_email` asks still holds: one canopy user
        provably holds the address and is a member of the agent's workspace.
    The turn keeps its real grade (`initiator_assurance`), so the caller
    envelope still says `verified: false`; it records the rule it rode on
    (`sender_trust`), and the turn's event ledger logs it.
    """

    #: Longest a rule may live. Long enough to get a domain's DKIM switched on.
    MAX_DAYS = 90

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="sender_trusts")
    email = models.EmailField(help_text="Exact From: address, lowercased.")
    reason = models.TextField(help_text="Why, and what fixes it for good.")
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "email"],
                                               name="uniq_agent_sender_trust")]
        ordering = ["email"]

    def __str__(self) -> str:  # pragma: no cover
        return f"sender-trust:{self.email}@{self.agent_id} until {self.expires_at:%Y-%m-%d}"

    @classmethod
    def active_for(cls, agent_id, email: str):
        """The unexpired rule for this agent and address, or None."""
        from django.utils import timezone

        return (cls.objects.filter(agent_id=agent_id, email=(email or "").strip().lower(),
                                   expires_at__gt=timezone.now()).first())


class EngagementEvent(models.Model):
    """One thing a person (or an agent) did to a project or a huddle — append-only.

    It exists to LEARN what works in multi-player mode (spec
    `2026-10-16-work-group-huddles-design.md` §7): does anyone open a proposed
    project, back it, comment on it, decide it — and through which surface. Ada
    reads `GET /api/engagement/summary` weekly and tells Jonathan in plain words
    what people actually do. So the table records acts, never content: `meta` is
    for small facts about the act (a stance, a reason's length), not the words.

    Written ONLY through `apps/agents/engagement.record`, which validates `kind`
    and `surface` and never lets a failed write break the caller's main path.

    `checkin` is a project check-in's id, as a string, not a foreign key: the
    model lives in `apps/huddles` and a row about a run must outlive the
    check-in it was about.
    `huddle_id` is a string for the same reason — a huddle is derived, not stored.
    """

    KINDS = ("viewed", "backed", "commented", "approved", "declined", "idea_added",
             "run_announced", "run_confirmed", "run_skipped", "run_expired", "proposed", "expired")
    SURFACES = ("web", "slack", "email", "mcp")

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="engagement_events")
    kind = models.CharField(max_length=16, choices=[(k, k) for k in KINDS])
    surface = models.CharField(max_length=8, choices=[(s, s) for s in SURFACES], default="web")
    #: Who did it: a person, an agent, or (machinery — an expiry) neither.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="engagement_events")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="engagement_events")
    #: What it was done to. SET_NULL: the record of an act outlives its object.
    project = models.ForeignKey(AgentProject, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="engagement_events")
    task = models.ForeignKey(AgentTask, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="engagement_events")
    huddle_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    checkin = models.CharField(max_length=64, blank=True, default="", db_index=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    meta = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "agents_engagement_event"
        ordering = ["-at", "-pk"]
        indexes = [models.Index(fields=["workspace", "at"]), models.Index(fields=["project", "at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.kind}@{self.surface}"
