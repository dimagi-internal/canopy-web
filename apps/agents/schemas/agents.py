from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Annotated, Literal

from canopy_transcript import closeout as transcript_closeout
from pydantic import Field, field_validator, model_validator

from apps.common.schemas import StrictModel

# framework→framework: agents and harness are both framework tier, and the
# source vocabulary has ONE definition (harness owns Turn.origin).
from apps.harness import clis
from apps.harness.schemas import LEGACY_SESSION_KEY, RoutableSource, adopt_legacy_session_key

# ---- Agent ----
CliName = clis.CliName


def _one_list_of_clis(value):
    if value is None:
        return value
    if not value:
        raise ValueError("name at least one CLI")
    if len(set(value)) != len(value):
        raise ValueError("a CLI is named twice")
    return value


class AgentIn(StrictModel):
    """Create or update an agent (upsert by slug)."""

    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    persona: str = ""
    email: str = Field(default="", max_length=254)
    # The agent's Google account (Drive, Docs, gog) when it is not `email` — e.g. a
    # canopy-hosted `<slug>@dimagi.ai` mailbox beside a `<slug>@dimagi-ai.com` Google
    # login. None leaves it unchanged (re-upserts omit it); "" clears it.
    google_account: str | None = Field(default=None, max_length=254)
    avatar_url: str = Field(default="", max_length=500)
    # Optional explicit home: a workspace slug. Setting it on an already-homed
    # agent MOVES it — the whole move, history included (apps/agents/move.py, the
    # same operation as POST /{slug}/move). Empty → an existing agent stays put.
    workspace: str = Field(default="", max_length=64)
    # Runtime-registry fields (Agent Runtime Registry). All None-defaulted and
    # written only when present, so the plugin's frequent re-upserts (which omit
    # them) never clobber runtime config back to empty. See services.upsert_agent.
    repo_url: str | None = Field(default=None, max_length=300)
    repo_ref: str | None = Field(default=None, max_length=120)
    # The agent CLIs it runs on, preferred first (`harness.clis`); None = unchanged.
    clis: list[CliName] | None = None
    # A model per CLI, e.g. {"codex": "gpt-6.1-sol"}; None = unchanged.
    cli_models: dict[CliName, Annotated[str, Field(max_length=64)]] | None = None
    runtime_secrets: list[str] | None = None
    # WHERE each declared secret's value lives: {"base-url": {"value": "https://..."}}
    # for a literal, {"name": {"local_only": true}} for one minted per machine.
    runtime_sources: dict | None = None
    # Ordered runner-kind preference, e.g. ["cloud","emdash"]. None = leave unchanged.
    runner_preference: list[str] | None = None

    @field_validator("clis")
    @classmethod
    def _clis_once_each(cls, value):
        return _one_list_of_clis(value)


class AgentMoveIn(StrictModel):
    """Where to move the agent: a workspace slug."""

    workspace: str = Field(min_length=1, max_length=64)


class AgentMoveTableOut(StrictModel):
    table: str
    rows: int
    # "re-homed", or "follows the agent" for a table with no workspace of its own.
    how: str


class AgentMoveLeftOut(StrictModel):
    table: str
    rows: int
    why: str


class AgentMoveBreakOut(StrictModel):
    # mail_route | runner | runner_order | shared_vault | slack | owner | admins |
    # agent_login | person_grants | admission_rules | chat_participants | turns_in_flight
    kind: str
    detail: str


class AgentMoveOut(StrictModel):
    """What a move carried (or, for a dry run, would carry), left and broke."""

    agent: str
    source: str
    destination: str
    dry_run: bool
    moved: bool
    tables: list[AgentMoveTableOut]
    left_behind: list[AgentMoveLeftOut]
    stops_working: list[AgentMoveBreakOut]


class AgentScaffoldIn(StrictModel):
    """A new agent's repository, stamped by the agent factory and pushed into an empty repo."""

    workspace: str = Field(min_length=1, max_length=64)
    slug: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    mandate: str = Field(min_length=1, max_length=500)
    mailbox: str = Field(default="", max_length=254)
    # The agent's Google account, when it differs from `mailbox`. Stamped into the repo's
    # config/agent.json `google_account` (as `canopy create-agent --google-account` does) and
    # set on the instance by the `upsert_agent` that follows.
    google_account: str = Field(default="", max_length=254)
    stakeholders: str = Field(default="", max_length=300)
    # `owner/name`, or its github.com URL. The person creates it, empty; canopy only pushes.
    repo: str = Field(min_length=3, max_length=200)
    # The person's fine-grained token for that repo — used for the push, never stored
    # here. The same token becomes the agent's delegation via PUT …/github.
    token: str = Field(min_length=1, max_length=400)


class AgentScaffoldOut(StrictModel):
    repo: str
    repo_url: str
    repo_ref: str
    # True when the repo already held this agent's whole scaffold, so nothing was pushed.
    adopted: bool
    # How many files were written (all of them for an empty repo; the missing ones otherwise).
    files: int


class RepoCheckIn(StrictModel):
    workspace: str = Field(min_length=1, max_length=64)
    slug: str = Field(min_length=1, max_length=80)
    repo: str = Field(min_length=3, max_length=200)
    # Empty is allowed: the answer still carries the links to make the repo and token.
    token: str = Field(default="", max_length=400)


class RepoCheckOut(StrictModel):
    repo: str
    state: Literal["ready", "update", "adopt", "name_mismatch", "not_empty", "missing", "no_token"]
    default_branch: str = "main"
    detail: str = ""
    # Who the token acts as on GitHub.
    login: str = ""
    # `update`: how many template files the repo lacks (only those are written).
    files_to_add: int = 0
    # `name_mismatch`: the slug this repo already names its agent.
    suggested_slug: str = ""
    # GitHub's new-repository form and its new-token form, filled in for this agent.
    new_repo_url: str = ""
    token_url: str = ""


class SetupLinkOut(StrictModel):
    href: str
    action: str


class AgentSetupStepOut(StrictModel):
    key: str
    title: str
    status: Literal["done", "todo", "waiting", "optional"]
    detail: str
    # web: `href` is the control that does it. claude-code: `command` starts the session.
    where: Literal["web", "claude-code"]
    href: str = ""
    # What following `href` does, as a button label: "Add a GitHub token".
    action: str = ""
    command: str = ""
    # Further choices beside `href`, each a link with its own label — the runner
    # step's "Pair your laptop" / "Stand up a cloud box" for someone who could pair one.
    links: list[SetupLinkOut] = []
    # Needed only for the agent to run on its own (a runner), not to use it in your
    # own Claude Code: an agent used that way alone is complete without this step.
    only_to_run_on_its_own: bool = False


class TurnModeIn(StrictModel):
    """Flip an agent's turn mode — a human decision, made from the board.

    Deliberately its own endpoint (not part of AgentIn): the agent-repo
    self-publish upsert must never be able to change its own autonomy."""

    turn_mode: Literal["manual", "auto"]


class AgentClisIn(StrictModel):
    """The agent CLIs an agent runs on, preferred first, and optionally a model for each."""

    clis: list[CliName]
    # None leaves the models as they are; {} clears them.
    cli_models: dict[CliName, Annotated[str, Field(max_length=64)]] | None = None

    @field_validator("clis")
    @classmethod
    def _clis_once_each(cls, value):
        return _one_list_of_clis(value)


class AgentHoldIn(StrictModel):
    # What you are tracing, so whoever finds the agent silent knows why and when to
    # release it — the fleet hold's note, for one agent.
    note: str = Field(default="", max_length=500)


class AgentHoldOut(StrictModel):
    held: bool
    note: str = ""
    held_at: dt.datetime | None = None
    held_by_email: str = ""
    # Turns waiting on the hold right now — what tried to reach the agent.
    queued: int = 0


class RepositoryIn(StrictModel):
    """Point an agent at its GitHub repository — the code its runners clone."""

    repo_url: str = Field(min_length=1, max_length=300)
    # The branch its runners run; empty leaves it as it is.
    repo_ref: str = Field(default="", max_length=120)


class SlackEnabledIn(StrictModel):
    """Turn Slack access to an agent on or off. Its own endpoint, for the same
    reason as TurnModeIn: the agent-repo upsert must not be able to open the
    agent to a new channel of people."""

    slack_enabled: bool


class TestVaultKeyIn(StrictModel):
    """Turn an agent login's TEST vault key on or off. Its own endpoint, never on the
    agent-repo upsert: an agent must not be able to give its own login a key."""

    enabled: bool


class TestVaultKeyOut(StrictModel):
    """Whether the agent's own login may hold a test vault key, and the key if one was
    made. `conversations` counts the sensitive conversations it holds a copy of the
    key to — the only thing such a key ever receives."""

    enabled: bool
    login_email: str = ""
    has_key: bool = False
    #: The key's thumbprint (RFC 7638), to tell one key from another.
    key_jkt: str = ""
    key_made_at: dt.datetime | None = None
    conversations: int = 0


class SlackEnabledOut(StrictModel):
    """The switch's new state, and what happened to the agent's `/<slug>` command.

    `command_status` is `synced` | `not_configured` | `error`. Reported rather
    than swallowed: a switch that left `/hal` unregistered would look exactly
    like one that worked, until somebody typed it."""

    slack_enabled: bool
    command_status: str
    command_detail: str = ""


class AgentRunnerOut(StrictModel):
    """One row of an agent's ordered runner list (the routing-matrix UI's read
    model). `online`/`ready` are computed per row from `Runner.live_status` /
    `Runner.ready` — not queryable columns, so the list stays tiny by design.
    `enabled=False` means the row is kept (rank preserved, shown greyed) but
    never routes — a toggle, not a removal."""

    runner_id: uuid.UUID
    runner_name: str
    kind: str
    rank: int
    online: bool
    ready: bool
    enabled: bool = True


class AgentPinnableRunnerOut(StrictModel):
    """A box the caller may start a chat with this agent on by choosing it. Its
    rank in the agent's default order when it is in it, None when it is not:
    choosing a box pins the chat there whatever the order and source rules say."""

    runner_id: uuid.UUID
    runner_name: str
    kind: str
    online: bool
    ready: bool
    rank: int | None = None


class AgentRoutePreviewRunnerOut(StrictModel):
    """One box in the composed list, in cascade order. `rung` is the rule that
    put it there: `person` (a rule for this person), `source` (a rule for this
    source) or `default` (the agent's runner order, or the workspace order it
    follows). `status` is `online`, `paused`, `offline` or `not ready`."""

    runner_id: uuid.UUID
    runner_name: str
    kind: str
    rung: str
    available: bool
    status: str


class AgentRoutePreviewOut(StrictModel):
    """Where this agent's work from `actor` on `source` would go right now, with
    no runner chosen. `picks` is the box that would claim it now (the first
    available one), None when it waits; `strict` when a rule says "these runners
    or nothing"; `follows_workspace` names the workspace order the agent follows
    when it has none of its own. `summary` says it in one line."""

    source: str
    actor: str
    runners: list[AgentRoutePreviewRunnerOut]
    picks: uuid.UUID | None = None
    strict: bool = False
    follows_workspace: str | None = None
    summary: str


class AgentDefaultOrderOut(StrictModel):
    """What an agent's "everything else" runs on. `own` True: `runners` is its
    own ordered list, the same rows as `GET /runners`. Otherwise it follows `workspace`'s default order —
    `runners` is that order as it applies to this agent, and two kinds of listed
    runner are left out, each named so a screen can say why: laptops without the
    agent's repo (`missing_repo`), boxes whose owner cannot hold the agent
    (`cannot_hold`) and directed-only boxes, which take no routed work
    (`directed`). With `own` True, `workspace` names the order it WOULD
    follow if its own list were cleared. `workspace` None: nothing to follow."""

    own: bool
    workspace: str | None = None
    runners: list[AgentRunnerOut] = []
    missing_repo: list[str] = []
    # The same laptops as `missing_repo`, by id, in the same order — what
    # `POST /api/harness/runners/{id}/projects` takes to add the repo there.
    missing_repo_ids: list[uuid.UUID] = []
    cannot_hold: list[str] = []
    directed: list[str] = []
    repo_url: str = ""


class AgentRunnerRowIn(StrictModel):
    """One row of the rows-form PUT body — carries `enabled` per runner,
    unlike the legacy all-enabled `runner_ids` form below."""

    runner_id: uuid.UUID
    enabled: bool = True


class AgentRunnersIn(StrictModel):
    """Wholesale replace of an agent's ordered runner list — index = rank.

    Exactly ONE of the two fields must be provided (422 otherwise): `runners`
    (ordered rows, each carrying its own `enabled`) or the legacy `runner_ids`
    (ordered ids, all implicitly enabled). Both fully replace the prior list —
    no partial-update ambiguity."""

    runner_ids: list[uuid.UUID] | None = None
    runners: list[AgentRunnerRowIn] | None = None


class AgentRunnerRuleOut(StrictModel):
    """One per-source routing rule: the priority runner for a source, and whether
    it is the ONLY runner allowed to take that source's work.

    `source` stays a plain `str` here (not the literal) by the same rule the rest
    of the API follows — an output schema serializes what the DB already holds, and
    a Literal would break on a value retired later. `queued_count` is the agent's
    queued turns from this source, which is what the UI's parked warning reads."""

    source: str
    # "" = the rule applies to any actor (what a source rule meant before actors).
    # Otherwise a normalized bare address. Plain `str` for the same reason `source`
    # is: an output schema serializes what the DB holds.
    actor: str = ""
    # Position WITHIN this rule. Rows sharing (source, actor) are one rule; the UI
    # groups on that pair and renders them as the same rank chip row the default
    # order uses.
    rank: int = 0
    runner_id: uuid.UUID
    runner_name: str
    kind: str
    strict: bool
    online: bool
    ready: bool
    enabled: bool = True
    queued_count: int = 0
    # manual | auto overrides the agent's turn mode for this rule's work; "" = the
    # rule says nothing about mode. Rule-level, repeated on every row like strict.
    turn_mode: str = ""


class AgentRunnerRuleIn(StrictModel):
    """One rule of the wholesale-replace body — a source, an optional actor, and
    the ORDERED runners that may take that work.

    `source` is typed as the routable literal so an unknown source is a 422 here
    rather than a rule that silently never matches anything — and so the generated
    TypeScript carries the union.

    `runners` reuses the default list's row schema, and its ORDER is the rule's
    rank order. A rule names several runners because the operator's own boxes are
    two macOS accounts alternated as each runs out of tokens: "either of mine,
    never cloud" cannot be said with one runner (spec 2026-09-05). Per-runner
    `enabled` lives on the row, which is why this schema has no rule-level
    `enabled` — a change from the pre-actor shape, safe because canopy-web's own
    frontend is the only caller and no rules exist in production yet.

    `strict` is rule-level and written to every row.
    """

    source: RoutableSource
    actor: str = ""
    runners: list[AgentRunnerRowIn] = Field(default_factory=list)
    strict: bool = False
    # "" leaves the mode to the next rung (the source rule, then the agent). An
    # `auto` on a rule that names a person applies only to a VERIFIED message
    # from them — see apps/harness/turn_mode.py.
    turn_mode: Literal["", "manual", "auto"] = ""


class AgentRunnerRuleBodyIn(StrictModel):
    """ONE rule, addressed by the URL (source, and `?actor=` for one person's):
    the runners in preference order, whether only they may take the work, and
    the turn mode. See PUT /agents/{slug}/runner-rules/{source}."""

    runners: list[AgentRunnerRowIn] = Field(default_factory=list)
    strict: bool = False
    turn_mode: Literal["", "manual", "auto"] = ""


class AgentRunnerRulesIn(StrictModel):
    """Wholesale replace of an agent's source rules. Scoped to non-empty-source
    rows: the default ordered list is the sibling endpoint's business, and neither
    write may clobber the other's rows."""

    rules: list[AgentRunnerRuleIn] = Field(default_factory=list)


class AgentActorRouteRunnerOut(StrictModel):
    runner_id: uuid.UUID
    runner_name: str
    online: bool
    enabled: bool = True


class AgentActorRouteOut(StrictModel):
    """One person's routing for an agent: whose work, which boxes, on which sources.

    A VIEW over the per-source actor rules, grouped by actor — the same rows
    `GET /runner-rules` lists one per (source, actor, runner). An actor whose
    sources route differently comes back as one entry per distinct routing."""

    actor: str
    runners: list[AgentActorRouteRunnerOut]
    strict: bool
    sources: list[str]
    turn_mode: str = ""
    queued_count: int = 0


class AgentActorRouteIn(StrictModel):
    """Route ONE person's work for this agent onto the given runners.

    `runners` is ordered — first is preferred. `strict` (default on) means only
    these runners may take the work: if they are all offline the turn waits rather
    than falling back to the agent's default order. `sources` defaults to every
    source that carries an actor; `canopy_scheduler` is refused because a
    schedule has no person behind it, so a rule on it could never match."""

    runners: list[AgentRunnerRowIn] = Field(default_factory=list)
    strict: bool = True
    sources: list[RoutableSource] | None = None
    turn_mode: Literal["", "manual", "auto"] = ""


class AgentRuntimeOut(StrictModel):
    """What a runner needs from canopy-web to run this agent: the repo pointer,
    the secret-reference names to resolve from the env store, the agent CLIs it
    runs on, and the tenant. Secret VALUES live in the secret store."""

    slug: str
    repo_url: str
    repo_ref: str
    secret_refs: list[str]
    workspace: str | None
    #: The agent CLIs it runs on, preferred first, and a model for each it names.
    clis: list[str] = ["claude-code"]
    cli_models: dict[str, str] = {}


class AgentPluginOut(StrictModel):
    """The plugin (code) an agent instance runs: its repo and catalog name."""

    repo: str = Field(description="`owner/repo` on github.com.")
    name: str = Field(description="The plugin's name in the `canopy-agents` catalog.")


class AgentOut(StrictModel):
    id: int
    slug: str
    name: str
    description: str
    persona: str
    email: str
    # The agent's Google account as stored ("" = the same as `email`, if that is Google).
    google_account: str = ""
    avatar_url: str
    created_at: dt.datetime
    updated_at: dt.datetime
    # The tenant that owns this agent — the fleet legitimately spans workspaces
    # (a chief-of-staff agent can live in a different tenant than the product
    # agents), so clients need this to build the correct deep link
    # (/w/<workspace>/agents/<slug>) instead of assuming the active workspace,
    # which 404s for cross-workspace agents (see commit 483c821).
    #
    # Aliased onto `workspace_id` rather than plain attribute resolution:
    # `Agent.workspace` is a FK, so a naive `obj.workspace` getattr would
    # dereference the related Workspace row (an extra query per agent) and
    # then fail str validation on the object itself. Workspace's primary key
    # IS its slug (apps/workspaces/models.py:29), so `agent.workspace_id` is
    # already the slug string with zero extra queries. Nullable for migration
    # safety, same as the FK itself.
    workspace: str | None = Field(default=None, validation_alias="workspace_id")
    # Ordered runner-kind preference (["cloud", "emdash", "remote"]). Empty = no
    # preference (any eligible runner, first-poll-wins). Lifted to the base schema
    # so the agents LIST (Page[AgentOut]) carries it — the supervisor Runners tab
    # maps it to "which agents prioritize which runner kind". Read directly from
    # the Agent.runner_preference JSONField by AgentOut.model_validate(agent);
    # AgentDetailOut inherits it.
    runner_preference: list[str] = Field(default_factory=list)
    # Runtime autonomy posture (manual | auto) — operational state a turn reads
    # at preflight, flipped from the board. On the base schema so both the
    # agents LIST and the detail view carry it. Literal so the generated TS
    # client gets the union, not string.
    turn_mode: Literal["manual", "auto"] = "manual"
    #: The agent CLIs it is set up for, its default first ("claude-code", "codex");
    #: a new chat or schedule may pick any of them.
    clis: list[str] = ["claude-code"]
    # Reachable from Slack (apps/slack). Owner-flipped, like turn_mode it is
    # operational state rather than repo config, so the self-publish upsert
    # cannot turn it on.
    slack_enabled: bool = False
    # The PLUGIN this instance runs, named exactly as the caller's
    # `canopy-agents` catalog names it (apps/plugins/services.py), so
    # `/plugin install <name>@canopy-agents` and `/<name>:setup` are right.
    # None when the agent has no github.com repo (it is in no catalog).
    plugin: AgentPluginOut | None = None


class AgentDefinitionOut(StrictModel):
    """What this instance RUNS, and who else runs it.

    An agent row is one tenant's instance; the definition is the repo it points
    at. Surfacing this is what makes "improving echo improves it everywhere"
    checkable rather than asserted — you can see whether a second tenant is on
    the same definition.
    """

    key: str = Field(
        description="Canonical identity of the repo, so two spellings of one "
                    "URL compare equal. Empty when the agent has no repo, which "
                    "means canopy cannot see its definition — not that it "
                    "shares one with other repoless agents.",
    )
    repo_url: str = ""
    repo_ref: str = ""
    shared_with: list[str] = Field(
        default_factory=list,
        description="Workspace slugs of OTHER instances running this same "
                    "definition. Cross-tenant on purpose — 'would this fix "
                    "reach them?' is a fleet question. Names tenants only: no "
                    "board, credentials or turns are disclosed.",
    )


class AgentOwnerOut(StrictModel):
    user_id: int
    name: str
    email: str


class AgentDetailOut(AgentOut):
    definition: AgentDefinitionOut | None = None
    #: A model per CLI; a CLI not named here starts on its own default.
    cli_models: dict[str, str] = {}
    # The person who operates this agent. Its GitHub-backed features (skill
    # history) read through THIS person's GitHub grant, so it is shown and
    # transferable in the UI. None when nobody has been assigned.
    owner: AgentOwnerOut | None = None
    #: The canopy user this agent IS (`Agent.user`) — the account its own token
    #: signs in as. None when unlinked.
    canopy_user: AgentOwnerOut | None = None
    #: A request to make someone's existing account this agent's login, waiting for
    #: THAT person to accept it in their own browser. None when none is open.
    canopy_user_request: "AgentLoginRequestOut | None" = None
    # Whether the CALLER may transfer ownership (a workspace owner, or the
    # agent's current owner). Drives whether the UI offers the control.
    can_transfer_owner: bool = False
    # Whether the CALLER is an admin of this agent (owner or explicit grant),
    # and whether they may grant/revoke admins (the agent's owner or a
    # workspace owner — granting admin hands over the agent's credentials).
    is_admin: bool = False
    can_manage_admins: bool = False
    sync_count: int = 0
    skill_count: int = 0
    task_count: int = 0
    turn_count: int = Field(
        default=0,
        description="Every turn of this agent's work, in any status: the same set "
        "`GET /api/harness/turns/?agent=` lists (turns targeting the agent, and "
        "email/Slack turns targeting one of its sessions), counted without that "
        "endpoint's page limit.",
    )
    latest_sync_at: dt.datetime | None = None
    latest_turn_at: dt.datetime | None = Field(
        default=None,
        description="When the agent last RAN, over the same set as `turn_count`: "
        "the newest `started_at`, or the newest `created_at` when none has started "
        "yet. A queued turn nobody claimed is not a run.",
    )


class AgentAdminOut(StrictModel):
    user_id: int
    email: str
    name: str
    # The agent's owner is always an admin and has no grant row; shown so the
    # list answers "who holds this agent's keys" completely.
    is_owner: bool = False
    granted_by_email: str | None = None
    granted_at: dt.datetime | None = None


class AgentLicenseOut(StrictModel):
    """One person's license to use the agent directly (`apps/agents/credentials/licenses.py`)."""

    user_id: int
    email: str
    name: str
    status: Literal["requested", "active", "revoked"]
    #: Why it was issued (`admin`, `grant:<line>`, `approved by …`).
    basis: str = ""
    note: str = ""
    requested_at: dt.datetime
    issued_at: dt.datetime | None = None
    issued_by_email: str | None = None
    revoked_at: dt.datetime | None = None
    revoked_by_email: str | None = None
    last_used_at: dt.datetime | None = None
    #: The private runners their direct turns on this agent ran on, most recent first.
    private_runners: list[str] = []
    local_turns: int = 0
    credential_reads: int = 0


class AgentLicenseRevokeIn(StrictModel):
    note: str = Field(default="", max_length=300)


class AgentAccessRowOut(StrictModel):
    user_id: int
    email: str
    name: str
    workspace_role: Literal["superuser", "owner", "admin", "editor", "viewer"]
    agent_role: Literal["owner", "admin", "member"]
    # Why they hold `agent_role`, in words: "Owns the workspace", "Made admin by …".
    basis: str
    granted_at: dt.datetime | None = None
    # What they reach signed in: the whole agent, only the listed capabilities,
    # or nothing (the published interface lists no capability for them).
    access: Literal["full", "confined", "none"]
    capabilities: list[str] = []
    # An editor line that says `manual_only` (docs/architecture/access.md): they reach the whole
    # agent, but every turn they start runs manual — outbound needs an admin.
    manual_only: bool = False
    # May ask for turn_mode=auto on a dispatch: the agent's owner and admins.
    may_request_auto: bool = False
    # WHERE they stand on the agent's role table (owner | admin | editor | read_write | read_only;
    # null when the agent lets them in nowhere, or only through a named capability) and what put them there (see
    # `AccessListOut`). `agent_role` above is the older three-word view and stays for
    # readers that have not moved.
    # `system` is the agent's own login: not a person, and not on the ladder.
    role: Literal["owner", "admin", "editor", "read_write", "read_only", "system"] | None = None
    source: str = ""
    # What put them there, in words ("Everyone in dimagi", "Anyone at @dimagi.com (verified)",
    # "Owns the workspace"); "" when it is just the role the workspace gives an editor.
    via: str = ""


class AgentAccessOut(StrictModel):
    members: list[AgentAccessRowOut]
    slack_enabled: bool


class AgentInterfaceIn(StrictModel):
    # Exactly one of: `source`, the YAML an editor wrote (kept, comments and all),
    # or `interface`, an already-parsed mapping. See apps/agents/interface.py.
    source: str | None = None
    interface: dict | None = None


class CapabilitySensitiveIn(StrictModel):
    # On writes `data: sensitive` into the capability's block of the saved YAML;
    # off removes it. An ordinary interface edit, as the YAML path is.
    handles_sensitive: bool


class InterfaceProblemOut(StrictModel):
    # The field the parser's message is about: `full`, a capability name, or
    # `<capability>.<field>`; "" when it is about the whole document.
    path: str
    message: str


class AgentInterfaceValidateOut(StrictModel):
    errors: list[InterfaceProblemOut]
    # When the draft is valid: the parsed model (what the form edits) and the same model as
    # YAML (what the Advanced view edits) — so the form and the YAML switch without a YAML
    # library in the browser.
    interface: dict | None = None
    yaml: str = ""


class ReadModeOut(StrictModel):
    """What a person who holds `read` and not `write` gets from this agent."""

    # `default`: canopy's own profile, on every agent from the start. `override`: the agent
    # published its own `ask`, which replaces the default whole.
    source: str
    # The published capability that overrides it (`ask`), else null.
    override: str | None = None
    # The default profile as it stands for this agent now, whether or not an override hides it.
    default_profile: dict
    default_profile_version: int


class AgentInterfaceOut(StrictModel):
    # Never empty: an agent without a declared interface has the default one.
    interface: dict
    read_mode: ReadModeOut
    # The YAML as last saved; "" when it was published as a parsed mapping.
    source: str = ""
    # `source` when there is one, else the interface rendered as YAML.
    yaml: str = ""
    # True while the interface is the default: no capability published.
    is_default: bool = True
    published_at: dt.datetime | None = None
    published_by_email: str | None = None


class AgentCanopyUserIn(StrictModel):
    #: True links the agent to the canopy account at its own email (the only account
    #: that can be its login); false unlinks it.
    link: bool


class AgentLoginRequestOut(StrictModel):
    """An open request to make a person's existing canopy account an agent's own login."""

    id: int
    agent_slug: str
    agent_name: str
    workspace_id: str
    #: The account to be linked — the only person who may accept.
    user_email: str
    requested_by_email: str = ""
    created_at: datetime
    expires_at: datetime


class AgentCanopyTokenIn(StrictModel):
    # The agent's mailbox address, used only to CREATE its login when it has none
    # and the agent's own `email` is blank. Ignored otherwise.
    email: str = ""
    label: str = ""


class AgentCanopyLoginOut(StrictModel):
    """`POST /canopy-login`: the agent's canopy login was made (or reused), its token
    stored sealed as `canopy-pat`, rotation turned on when it could be. Never the token."""

    user_email: str
    created_user: bool
    token_id: int
    rotation_enabled: bool
    # Why rotation stayed off, when it did ("" otherwise).
    rotation_note: str = ""


class AgentCanopyTokenOut(StrictModel):
    # Shown once and never stored here in the clear (PersonalToken keeps a hash).
    # The caller writes it to the agent's vault; it must not reach a transcript.
    raw: str
    token_id: int
    label: str
    user_email: str
    created_user: bool


class AgentOwnerIn(StrictModel):
    # None clears the owner (workspace owners only).
    user_id: int | None


# ---- Sync (Google-Doc backed) ----
class AgentSyncIn(StrictModel):
    period_start: dt.datetime
    period_end: dt.datetime
    title: str = Field(min_length=1, max_length=200)
    summary: str = ""
    doc_url: str = Field(min_length=1, max_length=500)
    self_grades: dict[str, str] = Field(default_factory=dict)
    source: str = Field(min_length=1, max_length=100)


class AgentSyncOut(StrictModel):
    id: int
    agent_slug: str
    period_start: dt.datetime
    period_end: dt.datetime
    title: str
    summary: str
    doc_url: str
    self_grades: dict[str, str] = Field(default_factory=dict)
    source: str
    created_at: dt.datetime


# ---- Turns (a packaged unit of work + optional transcript link) ----
#
# Backed by harness.Turn since 2026-08-11 — the dispatch record and the close-out
# report are one row. The wire names here are the CLOSE-OUT's names (`title`,
# `summary`, `source`), aliased onto the model's `report_*` fields so the whole
# fleet's `canopy agent turn` keeps working unchanged; on the model they need the
# prefix because a Turn also has dispatch-side prose (`prompt`, `result_note`).
class AgentTurnIn(StrictModel):
    cli_session_id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    summary: str = ""
    task_ext_ids: list[str] = Field(default_factory=list)
    work_product_urls: list[str] = Field(default_factory=list)
    session_slug: str = Field(default="", max_length=64)
    share_token: str = Field(default="", max_length=64)
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    source: str = Field(default="", max_length=100)
    # The session the turn ran in — how the server finds the dispatch row to attach
    # this report to (the runner stamps the same key). Optional: an agent that
    # cannot determine it still gets a report-only row, just an unjoined one. A
    # laptop agent recovers its emdash task from cwd (canopy's agent_client); a
    # cloud agent has none, and is joined on `cli_session_id` instead.
    session_key: str = Field(default="", max_length=200)
    # The turn being closed, when the agent knows it (the runner hands it the id in
    # its caller envelope). Wins over every other match: one Claude session can
    # serve several dispatched turns, and only the id tells them apart.
    turn_id: uuid.UUID | None = None
    emdash_task_id: str = LEGACY_SESSION_KEY
    # Structured provenance for the row — e.g. a huddle's anchor turn
    # (`{"kind": "huddle", "huddle": "<id>", ...}`, canopy `huddle`). MERGED into
    # the row's origin_ref: a re-post adds keys, it never drops the dispatch's own.
    origin_ref: dict = Field(default_factory=dict)

    _legacy_key = model_validator(mode="after")(adopt_legacy_session_key)


class AgentTurnOut(StrictModel):
    # A UUID now, not an int — Turn's pk. The agents surface never handed this id
    # back to the server for anything, so it is a display/key value only.
    id: uuid.UUID
    agent_slug: str
    cli_session_id: str
    title: str = Field(validation_alias="report_title")
    summary: str = Field(validation_alias="report_summary")
    task_ext_ids: list[str] = Field(default_factory=list)
    work_product_urls: list[str] = Field(default_factory=list)
    session_slug: str
    share_token: str
    # The transcript's page, under the workspace it was shared from — the only
    # address it has (canopy-web#1337). None when there is no transcript, its
    # token is not a live share, or the content is hidden from this reader.
    share_url: str | None = None
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = Field(default=None, validation_alias="finished_at")
    source: str = Field(validation_alias="report_source")
    created_at: dt.datetime
    # The dispatch half, now that it travels on the same row: `status`/`origin` say
    # whether the turn ran and what asked for it, and `reported_at` is how you tell
    # a turn the agent closed out from one that only ever got dispatched.
    status: str = ""
    origin: str = ""
    session_key: str = ""
    # Deprecated duplicate of `session_key`, for readers that predate the rename.
    emdash_task_id: str = Field(default="", validation_alias="session_key")
    reported_at: dt.datetime | None = None
    # The dispatch-side prose. Most turns are dispatched and run but never get a
    # close-out report (`reported_at` null, report_title/summary empty) — without
    # these the Turns page renders a list of bare dates with nothing to read.
    # `origin_ref` carries the scheduler slot / manual flag the origin label uses.
    prompt: str = ""
    result_note: str = ""
    # A PLAIN JOB's name and repo (blank for every other turn): its page shows the output as a log.
    job: str = ""
    job_repo: str = ""
    # The closeout verdict lifted off `result_note` (see harness TurnOut.closeout_verdict).
    closeout_verdict: str = ""
    closeout_line: str = ""
    origin_ref: dict = Field(default_factory=dict)
    # True when the content above (and `share_token`, the transcript's public
    # link) was blanked: a turn's content is a log (apps/harness/turn_access.py).
    content_hidden: bool = False
    # Where to see what the turn DID (harness.agent_report.list_turns). `chat_session_id` is
    # the chat holding its work — a chat turn's own session, or the session a
    # runner drove (an emdash session on a laptop, a recorded Claude session on a
    # cloud runner). A turn with no session falls back to its retained transcript
    # (`has_transcript`, read via /api/harness/turns/{id}/messages).
    chat_session_id: uuid.UUID | None = Field(default=None, validation_alias="linked_session_id")
    has_transcript: bool = False

    @model_validator(mode="after")
    def _closeout(self):
        self.closeout_verdict, self.closeout_line = transcript_closeout.find(self.result_note)
        return self
