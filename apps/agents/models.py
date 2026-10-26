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

from .interface_default import default_interface

#: An argument not passed, so read from the database. `None` is an answer (not
#: a member), so it cannot also mean "not given".
UNREAD = object()


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
    #: Never empty: an agent is created with the default interface (owner, admins and
    #: editors only), which `interface.is_default` recognises. See `apps/agents/interface.py`.
    interface = models.JSONField(default=default_interface, blank=True)
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
    # longer read by harness.services.claim_next_turn. Retained for AgentIn and
    # the one-time bridge in harness.routing that turns it into RunnerAssignment
    # rows; dropped with its own migration once nothing reads it.
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
    # Which backend `canopy cred` resolves this agent's secrets from (canopy#850).
    # `canopy-web` (canopy vault, zero-knowledge) is where every agent's secrets
    # live (Jonathan, 2026-10-21); `1password` is LEGACY, kept only for an agent
    # still holding a 1Password vault until an admin runs `canopy vault import
    # --zk --switch` — the runner/human then resolves through `op` with the
    # vault's key. Set only by the agent's owner or an admin
    # (PATCH /credential-source) — never by the repo's self-publish upsert. The
    # field default is only the bare model's: every created agent is born on canopy
    # vault (`services.born_zk_fields`).
    ONEPASSWORD, CANOPY_WEB = "1password", "canopy-web"
    CREDENTIAL_SOURCE_CHOICES = [(ONEPASSWORD, "1Password (legacy: op, via the agent's vault key)"),
                                 (CANOPY_WEB, "canopy vault (canopy-web)")]
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
    # `canopy-pat` every `pat_rotation_days` (apps/agents/credentials/pat_rotation.py).
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
    # docs/vault/zk-v1/zk_v1_reference.py. Owner/admin only (apps/agents/credentials/zk.py).
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
    # The agent's own login (`user`) may hold a personal vault key for TESTING, so a
    # headless harness signed in as the agent can take part in its own sensitive
    # conversations (apps/agents/credentials/test_key.py). Off by default; an agent
    # admin turns it on from a browser behind a passkey step-up. The key opens nothing
    # but conversation keys of conversations that login is in — never a vault.
    test_vault_key = models.BooleanField(default=False)
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

    def is_own_login(self, user) -> bool:
        """Is `user` this agent's OWN canopy login (`Agent.user`)?

        The one spelling of the check. An agent with no login matches nobody —
        `user_id is None` must never equal an anonymous caller's `pk` (also
        None), which a bare `agent.user_id == user.pk` lets through."""
        pk = getattr(user, "pk", None)
        return self.user_id is not None and pk is not None and self.user_id == pk

    def is_admin(self, user, *, role=UNREAD, granted: bool | None = None) -> bool:
        """The agent's owner, a WORKSPACE owner, or an explicit admin who is
        still a member of the workspace.

        `role` (their `member_role` in this workspace) and `granted` (they hold an
        `AgentAdmin` row) are read when not passed; a caller judging many people
        at once passes what it read in bulk (`access.Standings`).

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
        from apps.workspaces import role_cap
        from apps.workspaces import services as wsvc

        if role_cap.cap_of(user):
            return False  # acting as an editor or viewer (a turn's acts_as)
        if role is UNREAD:
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
        return granted if granted is not None else self.admin_grants.filter(user=user).exists()

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


class AgentLicense(models.Model):
    """A person's license to use an agent LOCALLY — "Aathithya uses ACE locally, since …".

    The agent's caller list (`access.decide`) still decides who MAY hold one; this row
    is who DOES (`apps/agents/licenses.py`, canopy-web#1704). Requested on first local
    use and issued at once when `decide` gives the person the whole agent; an admin
    revokes it per person without touching the rule, and a revoked license is not
    re-issued automatically — only an admin's approve brings it back. Local turns and
    credential reads point at it, so "who uses this agent outside canopy, and what did
    they read" is one query.
    """

    REQUESTED, ACTIVE, REVOKED = "requested", "active", "revoked"
    STATUS_CHOICES = [(REQUESTED, "Requested"), (ACTIVE, "Active"), (REVOKED, "Revoked")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="licenses")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="agent_licenses")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=REQUESTED)
    #: Why it was issued, in `access.decide`'s words: `admin`, `full:member@dimagi.com`,
    #: `editor`, or `approved by <email>`.
    basis = models.CharField(max_length=200, blank=True, default="")
    requested_at = models.DateTimeField(auto_now_add=True)
    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="+",
                                  help_text="Null when issued automatically by the caller list.")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    #: Why it was refused or revoked, for the person and the admins.
    note = models.CharField(max_length=300, blank=True, default="")
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_private_runner = models.ForeignKey("harness.PrivateRunner", on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "user"],
                                               name="one_license_per_agent_and_person")]
        ordering = ["-last_used_at", "requested_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"license:{self.agent_id}:{self.user_id}:{self.status}"


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
    #: `runner` (a box that runs the agent), `admin` (a human resolving by hand) or
    #: `license` (a license holder's local turn, `licenses.py`).
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


class Vault(models.Model):
    """A vault: a named set of secrets with its OWN membership (spec
    2026-10-22-vault-access-follows-canopy-access-design.md).

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


class VaultSecret(models.Model):
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
        constraints = [models.UniqueConstraint(fields=["vault", "name"], name="one_vault_secret_per_name")]

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
    below it (`workspace`, a subtree) — a turn of an agent reads every vault attached
    to it, layered under its own (`workspace_vaults.layered_for`) — or one cloud
    RUNNER, whose own Claude credentials it holds (`workspace_vaults.runner_layers`;
    spec 2026-10-23-runner-claude-credentials-in-the-vault-design.md)."""

    vault = models.ForeignKey("Vault", on_delete=models.CASCADE, related_name="attachments")
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="vault_attachments")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="vault_attachments")
    runner = models.ForeignKey("harness.Runner", on_delete=models.CASCADE, null=True, blank=True,
                               related_name="vault_attachments")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["vault_id", "created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(agent__isnull=False, workspace__isnull=True, runner__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=False, runner__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=True, runner__isnull=False)),
                name="vault_attachment_one_target"),
            models.UniqueConstraint(fields=["vault", "agent"], name="one_vault_attachment_per_agent",
                                    condition=models.Q(agent__isnull=False)),
            models.UniqueConstraint(fields=["vault", "workspace"], name="one_vault_attachment_per_subtree",
                                    condition=models.Q(workspace__isnull=False)),
            models.UniqueConstraint(fields=["vault", "runner"], name="one_vault_attachment_per_runner",
                                    condition=models.Q(runner__isnull=False)),
        ]

    def __str__(self) -> str:
        return f"{self.vault_id} -> {self.agent_id or self.workspace_id}"


class VaultMember(models.Model):
    """A person added to a vault by name — a direct membership, kept until an
    admin removes it, whatever happens to any agent the vault is linked to.

    A vault's roles are the agent's: `owner`, `admin`, `member` (no contacts). Owner
    is never stored: it comes only from owning the linked agent (or the workspace,
    for a workspace vault). A row or a link gives `admin` or `member`."""

    OWNER, ADMIN, MEMBER = "owner", "admin", "member"
    ROLE_CHOICES = [(ADMIN, "Admin"), (MEMBER, "Member")]

    vault = models.ForeignKey(Vault, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="vault_memberships")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=MEMBER)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

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
    #: Everyone with full access to any agent this (workspace) vault is attached to.
    ATTACHED = "attached"
    KIND_CHOICES = [(AGENT, "Full access to an agent"), (WORKSPACE_OWNERS, "Owners of a workspace"),
                    (WORKSPACE_ADMINS, "Admins of a workspace"), (ATTACHED, "Full access to an attached agent")]

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
      while its person owns the agent — or, for GitHub, is one of its admins and
      the owner has lent none (`delegations.delegation_for`) — so handing an
      agent to someone else stops the old owner's identity travelling with it
      unless they remain an admin. A vault item would have gone
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
    """A project: a piece of work with an end, owned by a workspace.

    **The workspace owns it** (`workspace`), and it is addressed there by its
    `handle`: `dimagi/unga-2026`, page `/w/dimagi/projects/unga-2026`. Any agent
    of the workspace (or of one above it) may file tasks into it; the agents
    working on it are its `member_agents`, and the person accountable is its
    lead (`owner_user`).

    **Its lead agent (`agent`) holds its files.** The layout in
    `agent-core/deliverables.md` is per agent: one Drive root per agent,
    `Projects/<name>/` beneath it. That is a good home for FILES and a poor one
    for state, so the folder stays in the lead agent's Drive and this row points
    AT it (`drive_folder_*`). A project nobody's agent leads has no folder yet.
    The lead agent may belong to the workspace or one above it (an org's agent
    leading a division's project: Eva, of dimagi, leads `connect/givewell-kb`).

    **`ext_id` is an agent's alias** ("P5"): projects were once numbered per
    agent, and the fleet still names them that way through
    `/api/agents/{slug}/projects/P5/`. The alias belongs to the agent that
    numbered it (`created_by_agent`) and survives a change of lead. A project
    made outside an agent's route has none.

    This is canopy's one project system. A project names a repo it touches only
    as a slug (`repo_slug`); there is no repo registry to point at.
    """

    #: `proposed` — an agent proposed it (from a huddle, a chat, its own turn) and
    #: nobody has started or declined it yet; its decision card ("Start this
    #: project?") is how a person does either (`apps/agents/proposals.py`).
    PROPOSED, PLANNED, ACTIVE, PAUSED = "proposed", "planned", "active", "paused"
    DONE, CANCELED, ARCHIVED = "done", "canceled", "archived"
    STATUS_CHOICES = [(PROPOSED, "Proposed"), (PLANNED, "Planned"), (ACTIVE, "Active"),
                      (PAUSED, "Paused"), (DONE, "Done"), (CANCELED, "Canceled"),
                      (ARCHIVED, "Archived")]
    #: Status -> category, the only thing progress, health and lists read.
    CATEGORY = {PROPOSED: "triage", PLANNED: "unstarted", ACTIVE: "started", PAUSED: "paused",
                DONE: "completed", CANCELED: "canceled", ARCHIVED: "canceled"}
    #: Statuses a project is still open in, in the order a list shows them.
    OPEN_STATUSES = (ACTIVE, PAUSED, PLANNED, PROPOSED)
    HANDLE_RE = r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$"

    #: The workspace that owns it. The lead agent's workspace or one below it
    #: (`services.may_hold_workspace_project`).
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="projects")
    #: Its address in its workspace ("givewell-kb"), lower case.
    handle = models.CharField(max_length=60)
    #: The LEAD AGENT: keeps it current and holds its Drive folder. Null for a
    #: project no agent leads.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="projects")
    #: The agents working on it. The lead agent is always one (`services`).
    member_agents = models.ManyToManyField(Agent, blank=True, related_name="member_projects")
    #: The agent whose `P<n>` alias `ext_id` is.
    created_by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="filed_projects")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="filed_projects")
    #: `created_by_agent`'s alias for it ("P5"), unique per that agent; blank for
    #: a project made outside an agent's route.
    ext_id = models.CharField(max_length=64, blank=True, default="",
                              help_text="An agent's alias (P1, P2 …), unique per that agent.")
    name = models.CharField(max_length=200, help_text="What the project is called — and its Drive folder's name.")
    outcome = models.TextField(blank=True, default="", help_text="What done looks like.")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=ACTIVE)
    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low — the same scale as a task's.
    priority = models.PositiveSmallIntegerField(default=0)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)

    #: The LEAD: the person accountable for the outcome. A real person where
    #: canopy knows them, so "waiting on you" can reach someone; free text
    #: (`owner_note`) otherwise.
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
    #: Where its knowledge base's objects are stored (`kb_store.root`): fixed for good,
    #: so a new handle or workspace moves nothing (#2047). Blank: `kb-projects/<pk>/`.
    #: A project made before then keeps the address-keyed root it stored under.
    storage_root = models.CharField(max_length=200, blank=True, default="")
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
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["created_by_agent", "ext_id"], condition=~models.Q(ext_id=""),
                                    name="project_agent_alias_unique"),
            models.UniqueConstraint(fields=["workspace", "handle"], name="project_handle_unique"),
            models.CheckConstraint(condition=~models.Q(handle=""), name="project_has_handle"),
        ]
        indexes = [models.Index(fields=["agent", "status"]), models.Index(fields=["workspace", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}/{self.handle} {self.name}"

    def save(self, *args, **kwargs):
        """Fill what a row may not lack, whoever builds it: its workspace (its
        lead agent's), a handle (free, from its name), and — carrying an alias —
        the agent the alias is of. Its lead agent is always a member."""
        creating = self.pk is None
        if not self.workspace_id and self.agent_id:
            self.workspace_id = self.agent.workspace_id
        if not self.handle:
            from .services import free_handle  # noqa: PLC0415 — services imports models

            self.handle = free_handle(self.workspace_id, self.name, fallback=self.ext_id or "project")
        if self.ext_id and not self.created_by_agent_id and creating:
            self.created_by_agent_id = self.agent_id
        super().save(*args, **kwargs)
        if creating and self.agent_id:
            self.member_agents.add(self.agent_id)

    # Schemas here are plain pydantic (`StrictModel`), not ninja `Schema`, so a
    # `resolve_*` on the schema is ignored — the models carry these instead, the
    # way `AgentTask.agent_slug` already does.
    @property
    def agent_slug(self) -> str | None:
        """Its lead agent's slug, or None when no agent leads it."""
        return self.agent.slug if self.agent_id else None

    @property
    def steward_slug(self) -> str | None:
        return self.agent_slug

    @property
    def category(self) -> str:
        return self.CATEGORY.get(self.status, "started")

    @property
    def member_slugs(self) -> list[str]:
        if hasattr(self, "_member_slugs"):
            return self._member_slugs
        if self.pk is None:
            return []
        return sorted(a.slug for a in self.member_agents.all())

    # Health and progress: a list route computes them for every row at once
    # (`work.annotate_projects`) and caches them here; a single project reads
    # its own.
    @property
    def health(self) -> str | None:
        if hasattr(self, "_health"):
            return self._health
        from .work import latest_update  # cycle: agents.work -> agents.models  # noqa: PLC0415

        latest = latest_update(self)
        return latest.health if latest else None

    @property
    def update_missing(self) -> bool:
        if hasattr(self, "_update_missing"):
            return self._update_missing
        from .work import update_missing  # cycle: agents.work -> agents.models  # noqa: PLC0415

        return update_missing(self)

    @property
    def done_task_count(self) -> int:
        cached = getattr(self, "_done_task_count", None)
        return self.tasks.filter(status=AgentTask.DONE).count() if cached is None else cached

    @property
    def address(self) -> str:
        """How every caller names it: `<workspace>/<handle>`."""
        return f"{self.workspace_id}/{self.handle}"

    @property
    def url(self) -> str:
        """Its one page."""
        return f"/w/{self.workspace_id}/projects/{self.handle}"

    @property
    def board_url(self) -> str:
        """Its page on its lead agent's board, in the AGENT's workspace (which for
        a division's project led from the parent is not the project's); `url`
        when no agent leads it."""
        if not self.agent_id:
            return self.url
        return f"/w/{self.agent.workspace_id}/agents/{self.agent.slug}/projects/{self.ext_id or self.handle}"

    # ---- what it serves (`ProjectService`) --------------------------------
    # A knowledge base is the one kind served from canopy itself; these read it
    # for the project's list shape (`AgentProjectOut`), so a page needs no second
    # fetch to show "serves a knowledge base".

    @property
    def knowledge_base(self) -> "ProjectService | None":
        """Its knowledge base, when it serves one (read once per instance)."""
        if not hasattr(self, "_knowledge_base"):
            cached = getattr(self, "_prefetched_objects_cache", {}).get("served")
            rows = cached if cached is not None else self.served.filter(kind=ProjectService.KB)
            self._knowledge_base = next((r for r in rows if r.kind == ProjectService.KB), None)
        return self._knowledge_base

    @property
    def mcp_kind(self) -> str:
        kb = self.knowledge_base
        return ProjectService.KB if kb is not None and kb.kb_key else ""

    @property
    def kb_published_at(self):
        kb = self.knowledge_base
        return kb.kb_published_at if kb else None

    @property
    def kb_version_number(self) -> int | None:
        kb = self.knowledge_base
        return kb.kb_version.number if kb and kb.kb_version_id else None

    @property
    def kb_drive_replica(self) -> bool:
        kb = self.knowledge_base
        return bool(kb and kb.kb_drive_replica)

    @property
    def mcp_path(self) -> str:
        """Where its own MCP server answers — a project serving a published
        knowledge base, else "". Under `/api/mcp/` so the same sign-in (a personal
        token, or OAuth) reaches it: one server per project, so a reader adds just
        this one and not the whole canopy surface."""
        if self.mcp_kind:
            return f"/api/mcp/projects/{self.workspace_id}/{self.handle}/"
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
        return {"agent": t.agent_slug or "", "ref": t.ref, "key": t.key, "title": t.title,
                "status": t.status,
                "project_ref": (t.project.ext_id or t.project.handle) if t.project_id else "",
                "project_name": t.project.name if t.project_id else "",
                "url": t.url}

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
        from apps.agents.services import waiting_q  # cycle: agents.services -> agents.models  # noqa: PLC0415

        return self.tasks.filter(waiting_q()).count()


class ProjectService(models.Model):
    """Something a project serves to its workspace (design 2026-10-24, "What a
    project serves"). A project is the workspace's and spans its agents and
    people, so it is where what they share hangs: its people may use it, its
    lead agent (or an editor) keeps it current, and the workspace directory
    (`GET /api/mcp-servers/`) lists it.

    Kinds:
      * `kb` — a knowledge base canopy serves as the project's own MCP server
        (`/api/mcp/projects/<ws>/<handle>/`), published by its lead agent's
        curator (`kb_store`, `kb_versions`). One per project; every `kb_*` field.
      * `mcp` — a remote MCP server the workspace shares: `url`, and how to add it.
      * `skill` — a Claude Code skill: its `instructions`, written here or
        published by the lead agent (`url`: where it came from, optional).
      * `plugin` — a Claude Code plugin (`url`: its repo) for the workspace.
    The projects connection (`apps/mcp/project_gateway.py`) serves every project's
    through one fixed set of tools, so nothing here reaches a session until the
    work asks for it. canopy serves `kb` and `skill`; an `mcp` or `plugin` is
    declared and listed, with how to add it.
    """

    KB, MCP, SKILL, PLUGIN = "kb", "mcp", "skill", "plugin"
    KINDS = (KB, MCP, SKILL, PLUGIN)
    KIND_CHOICES = [(KB, "Knowledge base"), (MCP, "MCP server"), (SKILL, "Skill"), (PLUGIN, "Plugin")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="served")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    #: Its name in the project ("kb" for the knowledge base; a server's, skill's
    #: or plugin's own name otherwise), unique per kind.
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True, default="")
    #: An MCP server's URL, a skill's or plugin's repository URL. Blank for a KB.
    url = models.URLField(max_length=500, blank=True, default="")
    #: A skill's instructions (its SKILL.md body): what an agent reads when it
    #: uses the skill. Blank for the other kinds.
    instructions = models.TextField(blank=True, default="")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- a knowledge base (kind `kb`) ------------------------------------------
    #: The published kb.sqlite.gz: its object key in the KB bucket, content hash
    #: (what a serving instance caches by) and size. Set by `kb_store.publish`.
    kb_key = models.CharField(max_length=300, blank=True, default="")
    kb_sha256 = models.CharField(max_length=64, blank=True, default="")
    kb_bytes = models.BigIntegerField(null=True, blank=True)
    kb_published_at = models.DateTimeField(null=True, blank=True)
    #: The head of its version log (`KbVersion`): the version `kb_key` serves. Null for
    #: a KB published before versions existed.
    kb_version = models.ForeignKey("agents.KbVersion", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    #: Off by default. On: after each version, canopy-web writes its kb.sqlite.gz into
    #: the project's Drive folder (`kb_versions`); no reader needs it — readers call the MCP.
    kb_drive_replica = models.BooleanField(default=False)
    kb_drive_replica_file_id = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "kind", "name"], name="uniq_project_service"),
            models.CheckConstraint(condition=models.Q(kind__in=["kb", "mcp", "skill", "plugin"]),
                                   name="project_service_kind_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.kind}:{self.name}"

    @property
    def kb_version_number(self) -> int | None:
        return self.kb_version.number if self.kb_version_id else None


class KbPassage(models.Model):
    """One passage of a workspace project's published knowledge base with its vector:
    where it is, what the vector was made from (`sha256` of
    `canopy_kb.vectors.embedding_text`) and the vector. DORMANT: nothing writes or
    reads these while KB_VECTORS is off (`kb_vectors.py`).

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
    """A task: one step of work, owned by a workspace and addressed by its KEY
    (`DIM-142`) — the workspace's prefix (`TaskSettings`) and its number there.

    **Assigned to a person or an agent** (decision 2026-10-24), never both: the
    agent doing it (`agent`) or the person (`assignee_user`), or nobody yet. An
    agent assignee is who an approval starts a turn for; a person assignee is
    who "My tasks" lists it for.

    **`ext_id` is an agent's alias** ("T12"): tasks were once numbered per agent
    and the fleet still names them so, through `/api/agents/{slug}/tasks/T12/`
    and the canopy CLI (which numbers them on the client). The alias belongs to
    the agent that filed it (`created_by_agent`) and survives reassignment. A
    task filed anywhere else has none.

    `status` values are fixed; each maps to a category (`CATEGORY`), which is
    what progress, counts and agents read. A workspace may rename how each
    category is SHOWN (`TaskSettings.status_names`)."""

    # Suggested = the agent proposed it; a person decides (Linear "Triage").
    # Backlog = accepted, not now. Todo = accepted, next up. "Waiting on a person"
    # is not a status: it is `waiting_on_user` or an open ask, shown as an overlay.
    SUGGESTED, BACKLOG, TODO, IN_PROGRESS = "suggested", "backlog", "todo", "in_progress"
    DONE, DECLINED = "done", "declined"
    #: An OPEN task — the one definition every count of "open tasks" uses.
    LIVE_STATUSES = (SUGGESTED, BACKLOG, TODO, IN_PROGRESS)
    STATUS_CHOICES = [
        (SUGGESTED, "Triage"),
        (BACKLOG, "Backlog"),
        (TODO, "Todo"),
        (IN_PROGRESS, "In progress"),
        (DONE, "Done"),
        (DECLINED, "Canceled"),
    ]
    #: Status -> category (Linear's six). Code that judges progress reads these.
    CATEGORY = {SUGGESTED: "triage", BACKLOG: "backlog", TODO: "unstarted",
                IN_PROGRESS: "started", DONE: "completed", DECLINED: "canceled"}
    #: Status order a list or board shows them in.
    STATUS_ORDER = (SUGGESTED, IN_PROGRESS, TODO, BACKLOG, DONE, DECLINED)

    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low (Linear's scale).
    PRIORITY_CHOICES = [(0, "No priority"), (1, "Urgent"), (2, "High"), (3, "Medium"), (4, "Low")]
    CONFIDENCE_VALUES = ("", "high", "medium", "low")

    #: The workspace that owns it.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="tasks")
    #: Its number in its workspace; with the prefix, its `key`.
    number = models.PositiveIntegerField()
    #: `<PREFIX>-<number>`, frozen when it is made (a prefix never changes once
    #: used), so a lookup by key is one indexed read.
    key = models.CharField(max_length=24, unique=True)
    #: The ASSIGNEE AGENT — the agent doing the work. Null when a person is the
    #: assignee, or nobody is.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="tasks")
    #: The ASSIGNEE PERSON. At most one of `agent` / `assignee_user` is set.
    assignee_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="assigned_tasks")
    #: Who filed it. `created_by_agent` also owns the `ext_id` alias.
    created_by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="filed_tasks")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="filed_tasks")
    ext_id = models.CharField(max_length=64, blank=True, default="",
                              help_text="An agent's alias (T1, T2 …), unique per the agent that filed it.")
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
    #: project per piece. Same workspace and project as the parent, no cycles,
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
    #: How sure the agent is about a suggestion: one of `CONFIDENCE_VALUES`.
    confidence = models.CharField(max_length=10, blank=True, default="")
    priority = models.PositiveSmallIntegerField(default=0, choices=PRIORITY_CHOICES)
    labels = models.ManyToManyField("agents.Label", blank=True, related_name="tasks")
    milestone = models.ForeignKey("agents.ProjectMilestone", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="tasks")
    #: Out of Triage until then — or until something new happens on it (an
    #: action or a field change clears it, `services`).
    snoozed_until = models.DateTimeField(null=True, blank=True)
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

    #: Order within a list or a board column: a fractional rank, so moving one
    #: task between two others writes one row (`services.rank_between`).
    rank = models.FloatField(default=0)
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]
        constraints = [
            # Ignoring case: an alias is looked up `ext_id__iexact`, so "t1" beside
            # "T1" would leave one of them unreachable.
            models.UniqueConstraint(Lower("ext_id"), "created_by_agent", condition=~models.Q(ext_id=""),
                                    name="uniq_task_agent_alias_ci"),
            models.UniqueConstraint(fields=["workspace", "number"], name="uniq_task_number"),
            models.CheckConstraint(condition=models.Q(agent__isnull=True) | models.Q(assignee_user__isnull=True),
                                   name="task_one_assignee"),
            models.CheckConstraint(condition=models.Q(confidence__in=["", "high", "medium", "low"]),
                                   name="task_confidence_known"),
            models.CheckConstraint(condition=models.Q(priority__lte=4), name="task_priority_known"),
        ]
        indexes = [models.Index(fields=["agent", "status"]), models.Index(fields=["workspace", "status"]),
                   models.Index(fields=["assignee_user", "status"])]

    def __str__(self):
        return f"task:{self.key}:{self.status}"

    def save(self, *args, **kwargs):
        """Fill what a row may not lack, whoever builds it: its workspace (its
        project's, else its parent's, else its agent's), its number and key
        there, and — carrying an alias — the agent the alias is of."""
        if not self.workspace_id:
            for src in (self.project if self.project_id else None, self.parent if self.parent_id else None,
                        self.agent if self.agent_id else None,
                        self.created_by_agent if self.created_by_agent_id else None):
                if src is not None:
                    self.workspace_id = src.workspace_id
                    break
        if self.ext_id and not self.created_by_agent_id and self.pk is None:
            self.created_by_agent_id = self.agent_id
        if not self.key:
            from .work import allocate_number  # noqa: PLC0415 — work imports models

            self.number, self.key = allocate_number(self.workspace_id)
        super().save(*args, **kwargs)

    @property
    def ref(self) -> str:
        """What an agent's route names it by: its alias where it has one, else its key."""
        return self.ext_id or self.key

    @property
    def url(self) -> str:
        """Its one page."""
        return f"/w/{self.workspace_id}/tasks/{self.key}"

    @property
    def category(self) -> str:
        return self.CATEGORY.get(self.status, "triage")

    @property
    def position(self) -> int:
        """`rank` as the integer older callers read."""
        return int(self.rank)

    @property
    def assignee_email(self) -> str | None:
        return self.assignee_user.email if self.assignee_user_id else None

    @property
    def created_by_agent_slug(self) -> str | None:
        return self.created_by_agent.slug if self.created_by_agent_id else None

    @property
    def label_list(self) -> list[dict]:
        """Its labels, from the prefetch cache when the list prefetched them."""
        if self.pk is None:
            return []
        return [{"name": lb.name, "color": lb.color} for lb in self.labels.all()]

    @property
    def milestone_name(self) -> str | None:
        return self.milestone.name if self.milestone_id else None

    @property
    def waiting_on_email(self) -> str | None:
        return self.waiting_on_user.email if self.waiting_on_user_id else None

    @property
    def ask_is_open(self) -> bool:
        return bool(self.ask_kind) and self.ask_closed_at is None

    @property
    def project_ext_id(self) -> str | None:
        """The project's agent alias where it has one, else its handle."""
        return (self.project.ext_id or self.project.handle) if self.project_id else None

    @property
    def project_handle(self) -> str | None:
        return self.project.handle if self.project_id else None

    @property
    def project_name(self) -> str | None:
        return self.project.name if self.project_id else None

    @property
    def project_address(self) -> str | None:
        """`<workspace>/<handle>`."""
        return self.project.address if self.project_id else None

    @property
    def parent_ext_id(self) -> str | None:
        return self.parent.ref if self.parent_id else None

    @property
    def parent_key(self) -> str | None:
        return self.parent.key if self.parent_id else None

    @property
    def agent_slug(self) -> str | None:
        """The assignee agent's slug, or None when an agent is not the assignee."""
        return self.agent.slug if self.agent_id else None

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
    """Something a person did to a task. While `pending` it is also the assignee
    agent's to-do: the agent drains pending rows on its next turn and marks each
    applied. The task's whole history is its `TaskEvent` stream, which links
    these rows."""

    APPROVE, DECLINE, REPLY, NUDGE, DONE = "approve", "decline", "reply", "nudge", "done"
    ACCEPT, BACKLOG, SNOOZE, DUPLICATE = "accept", "backlog", "snooze", "duplicate"
    ACTIONS = (APPROVE, DECLINE, REPLY, NUDGE, DONE, ACCEPT, BACKLOG, SNOOZE, DUPLICATE)
    ACTION_CHOICES = [(a, a) for a in ACTIONS]
    PENDING, APPLIED = "pending", "applied"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPLIED, "Applied")]

    #: The task's assignee agent when it was done — whose queue a pending row is
    #: on. Null on a task no agent is assigned.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="task_actions")
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
    def agent_slug(self) -> str | None:
        return self.agent.slug if self.agent_id else None

    @property
    def task_ext_id(self) -> str:
        return self.task.ref

    @property
    def task_key(self) -> str:
        return self.task.key


class TaskSettings(models.Model):
    """A workspace's task settings: the prefix its keys carry, the counter that
    numbers them, how each status category is shown, and how often an active
    project should post an update. Made on first use (`work.settings_for`)."""

    workspace = models.OneToOneField("workspaces.Workspace", primary_key=True, on_delete=models.CASCADE,
                                     related_name="task_settings")
    #: `DIM` in `DIM-142`. Upper-case letters and digits, unique across canopy so
    #: a key names one task anywhere; frozen once a task carries it.
    prefix = models.CharField(max_length=10, unique=True)
    #: The last number handed out. Only goes up: a reused number would make an
    #: old link point at new work.
    last_number = models.PositiveIntegerField(default=0)
    #: category -> the name this workspace shows it by ({"unstarted": "Ready"}).
    #: Missing categories use the default names.
    status_names = models.JSONField(default=dict, blank=True)
    #: An active project with no update in this many days shows "update missing".
    update_cadence_days = models.PositiveSmallIntegerField(default=7)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.prefix}"


class Label(models.Model):
    """A label tasks in one workspace share."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=60)
    #: A semantic colour token name (`primary`, `success`, `warning`,
    #: `destructive`, `info`, `muted`), never a raw palette value.
    color = models.CharField(max_length=20, blank=True, default="muted")
    description = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), "workspace", name="uniq_label_name_ci")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.name}"


class TaskRelation(models.Model):
    """`from_task` blocks / relates to / duplicates `to_task`. "Blocked by" is
    `blocks` read from the other end — never stored twice."""

    BLOCKS, RELATED, DUPLICATE = "blocks", "related", "duplicate"
    KINDS = (BLOCKS, RELATED, DUPLICATE)
    from_task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="relations_out")
    to_task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="relations_in")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in KINDS])
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["from_task", "to_task", "kind"], name="uniq_task_relation"),
            models.CheckConstraint(condition=~models.Q(from_task=models.F("to_task")),
                                   name="task_relation_not_self"),
            models.CheckConstraint(condition=models.Q(kind__in=["blocks", "related", "duplicate"]),
                                   name="task_relation_kind_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.from_task_id} {self.kind} {self.to_task_id}"


class TaskEvent(models.Model):
    """One entry in a task's history, append-only: it was created, a field
    changed, someone acted on it (the `AgentTaskAction` row, whose comment is
    the comment), or a relation was added or removed. Written by `work.record`
    from every writer, so the stream is the same whichever door the change came
    through."""

    CREATED, FIELD, ACTION, RELATION = "created", "field", "action", "relation"
    KINDS = (CREATED, FIELD, ACTION, RELATION)
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in KINDS])
    #: For `field`: which one, and its value before and after, as shown (a key,
    #: an email, a status) — the history must read the same after the related
    #: row is gone. For `relation`: the kind, and the other task's key in `new`
    #: (added) or `old` (removed).
    field = models.CharField(max_length=40, blank=True, default="")
    old = models.TextField(blank=True, default="")
    new = models.TextField(blank=True, default="")
    action = models.ForeignKey(AgentTaskAction, null=True, blank=True, on_delete=models.CASCADE,
                               related_name="events")
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["task", "created_at"])]
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["created", "field", "action", "relation"]),
                                              name="task_event_kind_known")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.task_id}:{self.kind}:{self.field}"


class ProjectMilestone(models.Model):
    """A checkpoint inside a project. Its progress is the share of its tasks done."""

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="milestones")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    target_date = models.DateField(null=True, blank=True)
    rank = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="uniq_milestone_name")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.name}"


class ProjectUpdate(models.Model):
    """A project's status update: its health, a short note, who wrote it, and
    what changed since the last one (`diff`, filled in when it is published).

    An agent may DRAFT one (a check-in run ends with one); a person PUBLISHES it,
    choosing the health — the agent proposes, the person decides. A project's
    health is its latest published update's."""

    ON_TRACK, AT_RISK, OFF_TRACK = "on_track", "at_risk", "off_track"
    HEALTHS = (ON_TRACK, AT_RISK, OFF_TRACK)
    DRAFT, PUBLISHED = "draft", "published"
    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="updates")
    health = models.CharField(max_length=10, choices=[(h, h) for h in HEALTHS])
    body = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=[(DRAFT, DRAFT), (PUBLISHED, PUBLISHED)], default=PUBLISHED)
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    author_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    checkin_run = models.ForeignKey("huddles.CheckinRun", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="updates")
    #: What changed since the previous published update: `{tasks_done, tasks_added,
    #: target_date: [old, new], milestones: [{name, done, total}]}`.
    diff = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["project", "status", "published_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(health__in=["on_track", "at_risk", "off_track"]),
                                   name="project_update_health_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.health}:{self.status}"

    @property
    def author_email(self) -> str | None:
        return self.author_user.email if self.author_user_id else None

    @property
    def author_agent_slug(self) -> str | None:
        return self.author_agent.slug if self.author_agent_id else None

    @property
    def published_by_email(self) -> str | None:
        return self.published_by.email if self.published_by_id else None


class SavedView(models.Model):
    """A saved task list: filters, grouping, sorting and layout under a name.
    Personal unless `shared`, when everyone in the workspace sees it."""

    TABLE, BOARD, CALENDAR = "table", "board", "calendar"
    LAYOUTS = (TABLE, BOARD, CALENDAR)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="saved_views")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=100)
    shared = models.BooleanField(default=False)
    #: The list's query, as the Tasks page puts it in its URL ({"assignee": "me",
    #: "category": "started,unstarted", "label": "bug"}).
    filters = models.JSONField(default=dict, blank=True)
    group_by = models.CharField(max_length=20, blank=True, default="")
    sort = models.CharField(max_length=20, blank=True, default="")
    layout = models.CharField(max_length=10, choices=[(x, x) for x in LAYOUTS], default=TABLE)
    rank = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.name}"


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

    The rule it bends is `harness.channels.email.member_behind_email`: a member's
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
      * everything else `member_behind_email` asks still holds: one canopy user
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


class KbVersion(models.Model):
    """One published state of a workspace project's knowledge base: the version log.

    A FULL publish is a whole kb.sqlite its steward built; canopy-web never builds one
    (#1971: the DELTA kind, which canopy-web built in its web process, is kept only in
    the log of versions made before). Each carries the curation state it was built from
    (`curation_sha`), which the next refresh starts from. A ROLLBACK points the project
    back at an earlier version's file and state. What changed is the file's own history
    (`whats_new`); `added`, `changed` and `removed` count it.
    """

    FULL, DELTA, ROLLBACK = "full", "delta", "rollback"
    KIND_CHOICES = [(FULL, "Full file"), (DELTA, "Delta"), (ROLLBACK, "Rollback")]
    PENDING, BUILDING, READY, FAILED = "pending", "building", "ready", "failed"
    STATUS_CHOICES = [(PENDING, "Pending"), (BUILDING, "Building"), (READY, "Ready"), (FAILED, "Failed")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_versions")
    number = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    #: The version this one was made from (a delta's base, a rollback's previous head).
    base = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: A rollback's target.
    rollback_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: The curation state it was built from: a root in the steward's results store
    #: (`canopy_kb.curate.State`). Blank for a file published without one (`canopy kb push`).
    curation_sha = models.CharField(max_length=64, blank=True, default="")
    #: Its kb.sqlite.gz. Cleared when pruned (`kb_versions.KEEP_FILES`): an older
    #: version stays in the log but can no longer be rolled back to.
    kb_key = models.CharField(max_length=300, blank=True, default="")
    kb_sha256 = models.CharField(max_length=64, blank=True, default="")
    kb_bytes = models.BigIntegerField(null=True, blank=True)
    added = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    removed = models.PositiveIntegerField(default=0)
    #: What the publisher sent for this version (records and parts), in bytes.
    uploaded_bytes = models.BigIntegerField(default=0)
    #: The built file's summary (`canopy_kb` summary: items, passages, …).
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    #: Who published it: the agent whose login sent it, else blank.
    published_by = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: The Drive replica's outcome when the project keeps one ("" when it does not).
    replica = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "agents_kb_version"
        ordering = ["project", "-number"]
        constraints = [models.UniqueConstraint(fields=["project", "number"], name="kb_version_number_unique")]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id} v{self.number} {self.kind} {self.status}"


class KbSource(models.Model):
    """Where a knowledge base's documents come from. Every document records its source
    (`meta.source` on its record); what happens after the source is shared by all.

    * `web` — a curator's crawler (GiveWell's, in eva). `config` notes what it reads.
      The only kind.
    """

    WEB = "web"
    KIND_CHOICES = [(WEB, "Web crawl")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_sources")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    name = models.CharField(max_length=100)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agents_kb_source"
        ordering = ["project", "id"]
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="kb_source_name_unique")]


class PreferredAgent(models.Model):
    """The agent a person wants an embedded panel to open on.

    One per person, everywhere: the same answer on every connected site and on
    canopy's own pages. It is a preference and grants nothing — a panel honours
    it only when the agent is one that panel may offer this person
    (`apps/embedding/defaults.py`), so a preference for an agent a site does not
    offer, or one the person can no longer reach, is passed over there.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="preferred_agent")
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agents_preferred_agent"

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.user_id} → {self.agent_id}"
