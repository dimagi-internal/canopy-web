"""Agent models: agent."""

from django.conf import settings
from django.db import models

from apps.harness import clis as clis_vocab

from .. import roles
from ..interface_default import default_interface

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
    #: Does this agent also take the access-list lines set on its workspace (and the
    #: workspaces above it)? On by default: that is how a workspace-wide or Dimagi-wide
    #: agent gets its audience from one place. An agent holding credentials people should
    #: be named for (Hal, Eva) turns it off and lists its own. See `apps/agents/grants.py`.
    inherit_workspace_defaults = models.BooleanField(default=True)
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
    # the POINTER to the agent's repo, the secret REFERENCES (names, never values;
    # declared in the Canopy vault by `canopy agent vault`), and the agent CLIs it
    # runs on. Secret values live in the store.
    repo_url = models.CharField(
        max_length=300, blank=True, default="", help_text="Agent's git repo.",
    )
    repo_ref = models.CharField(max_length=120, blank=True, default="main")
    # The agent CLIs this agent may run on, preferred first (`harness.clis`): the
    # claim picks the first one the box has signed in and can confine the turn on.
    # Part of the agent's definition, like its repo (agents.services.DEFINITION_FIELDS).
    clis = models.JSONField(default=clis_vocab.default_list, blank=True)
    # An optional model per CLI, e.g. {"codex": "gpt-6.1-sol"}; absent = the CLI's own default.
    cli_models = models.JSONField(default=dict, blank=True)
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
    # WHERE each declared secret's value comes from: {"<ref-name>": {"value": "..."}}
    # for a plain literal (a base URL), or {"<ref-name>": {"local_only": true}} for one
    # minted per machine. A real secret is never declared here: it is stored in the vault.
    runtime_sources = models.JSONField(
        default=dict, blank=True,
        help_text='Per-ref value source: {"name": {"value": "literal"}}.',
    )
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
    # Canopy Vault Phase 2 (canopy-web#1550): zero-knowledge, the agent vault's only
    # mode. Every AgentCredential holds ciphertext (`zk1:` under a per-agent vault
    # key canopy-web never sees, which reaches each principal as an AgentVaultWrap,
    # or `zks1` sealed to each reader before that key exists). `zk_kv` is the
    # current vault-key version (0 = no vault key yet). Formats:
    # docs/vault/zk-v1/zk_v1_reference.py (apps/agents/credentials/zk.py).
    #
    # Every agent is zero-knowledge — there is no other mode: a new one starts at kv 0
    # and its values are sealed (`zks1`) to its readers until a holder makes the key.
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
    # The agent hold: while `held`, no runner is handed this agent's turns — its own
    # (schedules, dispatches, mail) or a chat with it. The per-agent sibling of the
    # fleet hold (harness.FleetHold), enforced in the same one place,
    # `harness.claim.claim_next_turn`. Turns keep enqueuing and wait QUEUED, so the
    # queue shows what tried to reach the agent; running turns finish normally.
    # Operational state set from the board (POST /hold), never by the upsert.
    held = models.BooleanField(default=False)
    held_note = models.CharField(max_length=500, blank=True, default="")
    held_at = models.DateTimeField(null=True, blank=True)
    held_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="+")
    # The agent's own login (`user`) may hold a personal vault key for TESTING, so a
    # headless harness signed in as the agent can take part in its own sensitive
    # conversations (apps/agents/credentials/login_test_key.py). Off by default; an agent
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
        from .. import mailbox

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


class AgentRole(models.Model):
    """A named bundle of agent permissions an access-list line can give
    (`docs/superpowers/specs/2026-11-04-configurable-roles-design.md`).

    Defined on a workspace and usable by that workspace and every one below it; a key means one
    thing everywhere it is visible (the service refuses a clash along a chain). The permissions
    are the CLOSED set (`write` carries `read`, `agent_edit` carries `write`) chosen from the fixed
    vocabulary `read`, `write`, `agent_edit` and `use:<door>`: never `admin` or `own`, which come
    from `Agent.is_admin` and `Agent.owner`, not from a line. The built-ins (`read_only`,
    `read_write`, `editor`) are seeded rows: editable, never deletable.
    """

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="agent_roles")
    #: Immutable slug stored on every line that holds the role and sent in the caller envelope.
    key = models.CharField(max_length=40)
    label = models.CharField(max_length=80)
    description = models.CharField(max_length=300, blank=True, default="")
    permissions = models.JSONField(default=list)
    builtin = models.BooleanField(default=False)
    position = models.PositiveSmallIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "key"]
        constraints = [models.UniqueConstraint(fields=["workspace", "key"], name="uniq_agent_role_key")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.key} on {self.workspace_id}"


class AgentGrant(models.Model):
    """One line of an access list: `(who, role)`. See `apps/agents/grants.py`.

    A line sits on ONE agent, or on a workspace (`agent` null) where every agent in that
    workspace and below inherits it unless the agent opts out
    (`Agent.inherit_workspace_defaults`). `who` is a person, everyone in a workspace
    (optionally at least some workspace role there), or anyone whose address is at a
    domain. The highest role among a person's matching lines wins.
    """

    PERSON, WORKSPACE, DOMAIN, CONTACT = "person", "workspace", "domain", "contact"
    KIND_CHOICES = [(PERSON, "A person"), (WORKSPACE, "Everyone in a workspace"),
                    (DOMAIN, "Anyone at an email domain"),
                    (CONTACT, "A visitor or sender canopy knows, who is not a member")]

    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="grants")
    workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="agent_default_grants",
                                  help_text="Set instead of `agent`: a default for every agent here and below.")
    kind = models.CharField(max_length=12, choices=KIND_CHOICES)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.CASCADE, related_name="+")
    #: `kind=workspace`: the workspace whose people this names, and the lowest role there
    #: that counts ("" = any member); `domain` below, when set, narrows it to those whose
    #: address is at that domain.
    subject_workspace = models.ForeignKey("workspaces.Workspace", null=True, blank=True,
                                          on_delete=models.CASCADE, related_name="+")
    at_least = models.CharField(max_length=12, blank=True, default="")
    #: `kind=workspace`: also everyone in any workspace BELOW the subject (its own subtree in the
    #: parent tree, never another organisation's), at the same `at_least` / `domain`.
    include_below = models.BooleanField(default=False)
    #: `kind=domain`: anyone whose address is at the exact domain ("" = any address), member
    #: or not. `kind=contact`: a person canopy knows as a contact (an emailer, a Slack member
    #: outside the workspace, a widget visitor — who may have no address at all), optionally
    #: at a domain. `verified`: THIS message must prove the sender's address (mail that is
    #: DMARC-aligned or DKIM-signed by its own From: domain; Slack full-member); a signed-in
    #: request always does.
    domain = models.CharField(max_length=253, blank=True, default="")
    verified = models.BooleanField(default=True)
    workspace_delete_hint = "Remove the workspace's default access lines (Settings → Agent defaults) first."

    #: What the line grants: ONE role (`apps/agents/roles.py`) or, instead, ONE named capability
    #: (the scoped permission `use:<capability>`). Exactly one of the two is set.
    role = models.CharField(max_length=40, blank=True, default="")
    #: The role's row: what the line grants is the ROW's permissions, so editing the role changes its
    #: holders and nothing else moves. `role` is the row's key, kept as a copy (the envelope, the
    #: unique `subject_key`, the check constraint). Null exactly when `role` is "".
    role_def = models.ForeignKey(AgentRole, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="lines")
    capability = models.CharField(max_length=41, blank=True, default="")
    #: Every turn this line lets someone start runs `manual`: an admin approves what goes
    #: out. The editor lines migrated from "a workspace editor is an editor" keep it, so
    #: dropping the gate is one visible edit per agent.
    manual_only = models.BooleanField(default=False)
    #: What the line is for, in the words of whoever added it.
    note = models.CharField(max_length=300, blank=True, default="")
    #: `kind + the subject`, e.g. `person:12`, `workspace:connect:editor`, `domain:dimagi.com`.
    #: Computed on save; makes "one line per subject per scope" a plain unique constraint.
    subject_key = models.CharField(max_length=300, editable=False)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+",
                                   help_text="Null for a line seeded by migration.")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["granted_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(agent__isnull=False, workspace__isnull=True)
                           | models.Q(agent__isnull=True, workspace__isnull=False)),
                name="agent_grant_one_scope"),
            models.CheckConstraint(
                condition=(~models.Q(role="") & models.Q(capability=""))
                | (models.Q(role="") & ~models.Q(capability="")),
                name="agent_grant_role_or_capability"),
            models.CheckConstraint(
                condition=(models.Q(role="", role_def__isnull=True)
                           | (~models.Q(role="") & models.Q(role_def__isnull=False))),
                name="agent_grant_role_matches_row"),
            models.UniqueConstraint(fields=["agent", "subject_key"],
                                    condition=models.Q(agent__isnull=False),
                                    name="uniq_agent_grant_subject"),
            models.UniqueConstraint(fields=["workspace", "subject_key"],
                                    condition=models.Q(workspace__isnull=False),
                                    name="uniq_workspace_grant_subject"),
        ]

    @staticmethod
    def key_of(kind, *, user_id=None, subject_workspace=None, at_least="", domain="",
               include_below=False, capability="") -> str:
        if kind == AgentGrant.PERSON:
            key = f"person:{user_id}"
        else:
            d = (domain or "").lower()
            if kind == AgentGrant.WORKSPACE:
                key = (f"workspace:{getattr(subject_workspace, 'pk', subject_workspace)}:{at_least}:{d}"
                       + (":below" if include_below else ""))
            elif kind == AgentGrant.CONTACT:
                key = f"contact:{d}"
            else:
                key = f"domain:{d}"
        # A capability line is one line per capability: the same subject can hold a role line and
        # several capability lines, so the capability is part of what makes a line unique.
        return f"{key}#use:{capability}" if capability else key

    @property
    def permissions(self) -> frozenset[str]:
        """What the line grants: its role's ROW (closed), or the one `use:<capability>`. Read the
        lines with `select_related("role_def")` (`grants.lines_for*`) so this is no query."""
        if not self.role:
            return frozenset({roles.use(self.capability)})
        if self.role_def_id is not None:
            return roles.ROLES.close(self.role_def.permissions)
        return frozenset()   # a role with no row grants nothing (never guessed from its name)

    def save(self, *args, **kwargs):
        self.subject_key = self.key_of(
            self.kind, user_id=self.user_id, subject_workspace=self.subject_workspace_id,
            at_least=self.at_least, domain=self.domain, include_below=self.include_below,
            capability=self.capability)
        if self.domain:
            self.domain = self.domain.lower()
        kwargs = self._link_role(kwargs)
        super().save(*args, **kwargs)

    def _link_role(self, kwargs: dict) -> dict:
        """Point `role_def` at the row for `role` when the caller did not (a line made in a test, a
        patch that changed `role`): resolved along the line's own chain. Routes pass the row."""
        if not self.role:
            self.role_def = None
        elif self.role_def_id is None or self.role_def.key != self.role:
            from .. import rolecatalog  # noqa: PLC0415 — the catalog imports these models

            home = self.workspace_id or (self.agent.workspace_id if self.agent_id else None)
            self.role_def = rolecatalog.role_for(home, self.role) if home else None
        fields = kwargs.get("update_fields")
        if fields is not None and ("role" in fields or "role_def" in fields) and "role_def" not in fields:
            kwargs = {**kwargs, "update_fields": [*fields, "role_def"]}
        return kwargs

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.subject_key} -> {self.role} on {self.agent_id or self.workspace_id}"


class AgentLicense(models.Model):
    """A person's license to use an agent DIRECTLY — "Aathithya uses ACE directly, since …".

    The agent's caller list (`access.decide`) still decides who MAY hold one; this row
    is who DOES (`apps/agents/credentials/licenses.py`). Requested on first local
    use and issued at once when `decide` gives the person the whole agent; an admin
    revokes it per person without touching the rule, and a revoked license is not
    re-issued automatically — only an admin's approve brings it back. Direct turns and
    credential reads point at it, so "who uses this agent outside canopy, and what did
    they read" is one query.
    """

    REQUESTED, ACTIVE, REVOKED = "requested", "active", "revoked"
    STATUS_CHOICES = [(REQUESTED, "Requested"), (ACTIVE, "Active"), (REVOKED, "Revoked")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="licenses")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="agent_licenses")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=REQUESTED)
    #: Why it was issued, in `access.decide`'s words: `admin`, `grant:<access-list line>`, or
    #: `approved by <email>`.
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
    #: agent's turns (2026-10-04), so this says which path the token came through.
    gog_client = models.CharField(max_length=120, blank=True, default="")
    #: The client the agent's TURNS will actually present — what
    #: `config/agent.json.gog_client` declares (`canopy` fleet-wide), or — when a
    #: token under the OTHER fleet client serves it — the client turns will
    #: actually resolve to (canopy `reconcile_client`). Distinct from `gog_client`
    #: above: that one is whichever client the box's token was minted under.
    turn_client = models.CharField(max_length=120, blank=True, default="")
    #: Did the agent's secrets materialize (`canopy cred sync` writing its `.env`)?
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
    # `user-invocable: false` in the skill's file, as a runner reports it
    # (`skill_catalog`): a real skill Claude loads, never offered as a `/` command.
    # Counted in the catalog, so nothing appears to have vanished. Set only from a
    # report, on a published catalog too, since a publish does not say it.
    internal = models.BooleanField(
        default=False, help_text="Claude loads this skill; a person is not offered it as a command."
    )
    # Who wrote the row: the agent itself (`PUT …/skills/`) or canopy-web from what
    # a runner reports it can run (`skill_catalog`, which merges a report into the
    # catalog and keeps what a published row alone can say).
    PUBLISHED, FROM_RUNNER = "published", "runner"
    source = models.CharField(max_length=16, default="published")
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
    project = models.ForeignKey("agents.AgentProject", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="engagement_events")
    task = models.ForeignKey("agents.AgentTask", null=True, blank=True, on_delete=models.SET_NULL,
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
