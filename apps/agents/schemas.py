"""Pydantic schemas for the /api/agents surface."""
from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

from apps.agents.models import AgentTask
from apps.common.schemas import StrictModel

# framework→framework: agents and harness are both framework tier, and the
# source vocabulary has ONE definition (harness owns Turn.origin).
from apps.harness.schemas import (
    LEGACY_SESSION_KEY,
    RoutableSource,
    TurnSpecIn,
    adopt_legacy_session_key,
    normalize_origin,
)


# ---- Agent ----
class AgentIn(StrictModel):
    """Create or update an agent (upsert by slug)."""

    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    persona: str = ""
    email: str = Field(default="", max_length=254)
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
    runtime_engine: Literal["emdash", "cloud_p", "any"] | None = None
    runtime_secrets: list[str] | None = None
    # WHERE each declared secret's value lives, mirrored from runtime.yaml:
    # {"gog-token": {"op": "op://Agent-Ace/gog-token/credential"}}. Without it
    # the vault importer can only guess a convention, and a guess here resolves
    # to the WRONG credential rather than failing (ace#2060).
    runtime_sources: dict | None = None
    # Ordered runner-kind preference, e.g. ["cloud","emdash"]. None = leave unchanged.
    runner_preference: list[str] | None = None


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


class AgentSetupStepOut(StrictModel):
    key: str
    title: str
    status: Literal["done", "todo", "waiting", "optional"]
    detail: str
    # web: `href` is the control that does it. claude-code: `command` starts the session.
    # terminal: `command` is a shell command a person runs (one that needs their own
    # access, e.g. 1Password rights) — not a Claude Code session.
    where: Literal["web", "claude-code", "terminal"]
    href: str = ""
    # What following `href` does, as a button label: "Add a GitHub token".
    action: str = ""
    command: str = ""


class RunnerPreferenceIn(StrictModel):
    """Set an agent's ordered runner-kind preference (the runner-order UI)."""

    runner_preference: list[str] = Field(default_factory=list)


class TurnModeIn(StrictModel):
    """Flip an agent's turn mode — a human decision, made from the board.

    Deliberately its own endpoint (not part of AgentIn): the agent-repo
    self-publish upsert must never be able to change its own autonomy."""

    turn_mode: Literal["manual", "auto"]


class CredentialSourceIn(StrictModel):
    """Choose where `canopy cred` resolves this agent's secrets from (canopy#850).

    Its own endpoint, owner/admin only, for the same reason as TurnModeIn: the
    agent-repo self-publish upsert must never be able to move its own secrets."""

    credential_source: Literal["1password", "canopy-web"]


class SlackEnabledIn(StrictModel):
    """Turn Slack access to an agent on or off. Its own endpoint, for the same
    reason as TurnModeIn: the agent-repo upsert must not be able to open the
    agent to a new channel of people."""

    slack_enabled: bool


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



class AgentDefaultOrderOut(StrictModel):
    """What an agent's "everything else" runs on. `own` True: `runners` is its
    own ordered list, the same rows as `GET /runners`. Otherwise it follows `workspace`'s default order —
    `runners` is that order as it applies to this agent, and two kinds of listed
    runner are left out, each named so a screen can say why: laptops without the
    agent's repo (`missing_repo`) and boxes whose owner cannot hold the agent
    (`cannot_hold`). With `own` True, `workspace` names the order it WOULD
    follow if its own list were cleared. `workspace` None: nothing to follow."""

    own: bool
    workspace: str | None = None
    runners: list[AgentRunnerOut] = []
    missing_repo: list[str] = []
    cannot_hold: list[str] = []
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
    """What a runner needs from canopy-web to run this agent: the repo pointer
    (whose runtime.yaml is the declarative spec), the secret-reference names to
    resolve from the env store, the engine preference, and the tenant. The
    declarative spec + secret VALUES live elsewhere (repo / secret store)."""

    slug: str
    repo_url: str
    repo_ref: str
    engine: str
    secret_refs: list[str]
    workspace: str | None


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
    # Reachable from Slack (apps/slack). Owner-flipped, like turn_mode it is
    # operational state rather than repo config, so the self-publish upsert
    # cannot turn it on.
    slack_enabled: bool = False
    # Which backend `canopy cred` resolves this agent's secrets from (canopy#850).
    # Owner/admin-set via PATCH /credential-source; absent from AgentIn so the
    # self-publish upsert cannot move an agent's secrets.
    credential_source: Literal["1password", "canopy-web"] = "1password"
    # Canopy Vault device binding (canopy-web#1538): with credential_source
    # canopy-web, resolve also demands a DPoP proof from an approved runner
    # device key. Owner/admin-set via PATCH /device-binding.
    require_device_binding: bool = False
    # The INSTANCE's 1Password vault NAME (e.g. "Agent-Ace") — what a local
    # install bound to this instance sets itself up from. A name, not a secret:
    # the vault's service-account token (`op_sa_token_enc`) is never on this
    # schema, and 1Password itself decides who can read the vault. "" = none
    # recorded. Same visibility as the row (members of the agent's workspace).
    op_vault: str = ""
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
    # The person who operates this agent. Its GitHub-backed features (skill
    # history) read through THIS person's GitHub grant, so it is shown and
    # transferable in the UI. None when nobody has been assigned.
    owner: AgentOwnerOut | None = None
    #: The canopy user this agent IS (`Agent.user`) — the account its own token
    #: signs in as. None when unlinked.
    canopy_user: AgentOwnerOut | None = None
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


class AgentAccessRowOut(StrictModel):
    user_id: int
    email: str
    name: str
    workspace_role: Literal["owner", "admin", "editor", "viewer"]
    agent_role: Literal["owner", "admin", "member"]
    # Why they hold `agent_role`, in words: "Owns the workspace", "Made admin by …".
    basis: str
    granted_at: dt.datetime | None = None
    # What they reach signed in: the whole agent, only the listed capabilities,
    # or nothing (the published interface lists no capability for them).
    access: Literal["full", "confined", "none"]
    capabilities: list[str] = []
    # The `full:` interface rule that lifted a member to full access, if one did.
    full_rule: str | None = None
    # The editor tier (docs/architecture/access.md): a workspace editor who is
    # not an agent admin reaches the whole agent, but every turn they start runs
    # manual — outbound needs an admin.
    manual_only: bool = False
    # May ask for turn_mode=auto on a dispatch: the agent's owner and admins.
    may_request_auto: bool = False


class AgentOutsiderRuleOut(StrictModel):
    caller: str
    access: Literal["full", "confined"]
    capability: str | None = None


class AgentAccessOut(StrictModel):
    members: list[AgentAccessRowOut]
    # Rules reaching people outside the workspace (contacts, unidentified).
    outsiders: list[AgentOutsiderRuleOut]
    # False = no interface: only the agent's admins and workspace editors reach
    # it (editors manual only); viewers and everyone outside get nothing.
    interface_published: bool
    slack_enabled: bool


class AgentInterfaceIn(StrictModel):
    # Exactly one of: `source`, the YAML an editor wrote (kept, comments and all),
    # or `interface`, an already-parsed mapping. See apps/agents/interface.py.
    source: str | None = None
    interface: dict | None = None


class AgentInterfaceOut(StrictModel):
    # Empty when nothing is published: every turn runs in the full profile.
    interface: dict
    # The YAML as last saved; "" when it was published as a parsed mapping.
    source: str = ""
    published_at: dt.datetime | None = None
    published_by_email: str | None = None
    # Rules that cannot work as written, e.g. `member@<domain>` for a domain whose
    # mail can never prove its sender (no DMARC, no aligned DKIM). Advisory: the
    # interface is saved regardless.
    warnings: list[str] = []


class AgentCanopyUserIn(StrictModel):
    # None unlinks.
    user_id: int | None


class AgentCanopyTokenIn(StrictModel):
    # The agent's mailbox address, used only to CREATE its login when it has none
    # and the agent's own `email` is blank. Ignored otherwise.
    email: str = ""
    label: str = ""


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
    origin_ref: dict = Field(default_factory=dict)
    # True when the content above (and `share_token`, the transcript's public
    # link) was blanked: a turn's content is a log (apps/harness/turn_access.py).
    content_hidden: bool = False
    # Where to see what the turn DID (services.list_turns). `chat_session_id` is
    # the chat holding its work — a chat turn's own session, or the session a
    # runner drove (an emdash session on a laptop, a recorded Claude session on a
    # cloud runner). A turn with no session falls back to its retained transcript
    # (`has_transcript`, read via /api/harness/turns/{id}/messages).
    chat_session_id: uuid.UUID | None = Field(default=None, validation_alias="linked_session_id")
    has_transcript: bool = False


# ---- Skill catalog ----
class AgentSkillIn(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    url: str = Field(default="", max_length=500)
    improvement_note: str = ""
    # Fail closed: a publish that predates these fields marks nothing launchable.
    launchable: bool = False
    args_hint: str = Field(default="", max_length=120)


class AgentSkillCatalogIn(StrictModel):
    """Full replacement of the agent's skill catalog."""

    skills: list[AgentSkillIn] = Field(default_factory=list)


class AgentSkillOut(StrictModel):
    id: int
    agent_slug: str
    name: str
    description: str
    url: str
    improvement_note: str
    launchable: bool
    args_hint: str
    updated_at: dt.datetime


# ---- projects and tasks ----
class AgentTaskLink(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)
    #: `decision`: an open question this task is carrying for a person — the
    #: project's team view lists each one as a line under "waiting on". Blank
    #: for an ordinary link.
    #: `live`: on a PROJECT, what the product already does today — its label
    #: says what, its url opens it. The project page leads "Where it stands"
    #: with these, so a live product never reads as "nothing built" because
    #: none of its ideas is marked shipped.
    kind: Literal["", "decision", "live"] = ""
    #: On a decision: who it waits on, by name. Blank falls back to the task's
    #: waiting-on person, then the project's owner.
    waiting_on: str = Field(default="", max_length=120)


class HumanWho(StrictModel):
    """Who said it. Give `email` (and `name`): canopy links the Person it names
    when it knows them in this agent's workspace. `person_id` names one
    directly. At least one of the three."""

    person_id: int | None = None
    email: str = Field(default="", max_length=254)
    name: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _someone(self):
        if self.person_id is None and not self.email.strip() and not self.name.strip():
            raise ValueError("who needs an email, a name or a person_id")
        return self


class HumanWhyIn(StrictModel):
    """What a person actually said that this task answers (T68): their words,
    verbatim — never a paraphrase — who said them, and where."""

    quote: str = Field(min_length=1, max_length=4000)
    who: HumanWho
    source_url: str = Field(default="", max_length=500)
    said_at: dt.datetime | None = None


class HumanWhoOut(StrictModel):
    #: Set when canopy linked a Person (known in the agent's workspace).
    person_id: int | None = None
    email: str = ""
    name: str = ""


class HumanWhyOut(StrictModel):
    quote: str
    who: HumanWhoOut
    source_url: str = ""
    said_at: dt.datetime | None = None


class AgentProjectIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    ext_id: str = Field(default="", max_length=64)
    outcome: str = ""
    status: str = "active"
    owner_note: str = Field(default="", max_length=200)
    # The owner as a real person — a member of the agent's workspace — so an
    # idea added on the team view can email them. Unknown → 422.
    owner_email: str = Field(default="", max_length=254)
    drive_folder_id: str = Field(default="", max_length=128)
    drive_folder_url: str = Field(default="", max_length=500)
    repo_slug: str = Field(default="", max_length=100)
    notes: str = ""
    links: list[AgentTaskLink] = Field(default_factory=list)


class AgentProjectPatch(StrictModel):
    name: str | None = Field(default=None, max_length=200)
    outcome: str | None = None
    status: str | None = None
    owner_note: str | None = Field(default=None, max_length=200)
    # Present-and-empty clears the linked owner; absent leaves it.
    owner_email: str | None = Field(default=None, max_length=254)
    drive_folder_id: str | None = Field(default=None, max_length=128)
    drive_folder_url: str | None = Field(default=None, max_length=500)
    repo_slug: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    links: list[AgentTaskLink] | None = None
    #: The task this project grew out of, as `<agent>/<task ext_id>` ("ada/T41");
    #: `""` clears it. Same agent's or another visible agent's task.
    spawned_from: str | None = Field(default=None, max_length=130)


class SpawnedFromOut(StrictModel):
    """The task a project grew out of: "from <task> in <project>"."""

    agent: str
    ref: str
    title: str
    status: str
    project_ref: str = ""
    project_name: str = ""
    #: The page to open: the task's project, else its agent's board.
    url: str


class AgentProjectOut(StrictModel):
    id: int
    agent_slug: str
    #: The workspace the project lives in (its agent's). A list pinned to a
    #: parent workspace carries its children's rows too.
    workspace: str = ""
    ext_id: str
    name: str
    outcome: str
    status: Literal["proposed", "active", "done", "archived"]
    owner_note: str
    owner_email: str | None = None
    drive_folder_id: str
    drive_folder_url: str
    repo_slug: str
    notes: str
    links: list[AgentTaskLink] = Field(default_factory=list)
    task_count: int = 0
    open_task_count: int = 0
    #: Live tasks with an open ask or parked on a person (`services.waiting_q`).
    waiting_task_count: int = 0
    #: The task this project grew out of, if it did.
    spawned_from: SpawnedFromOut | None = Field(
        default=None, validation_alias=AliasChoices("spawned_from_ref", "spawned_from"))
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentTaskIn(StrictModel):
    """One task to create. `POST /tasks/` takes a LIST of these.

    `idempotency_key` makes a create safe to retry: a key already seen returns
    the task it made instead of making another. `ext_id` is assigned (T1, T2 …)
    when omitted — pass it only to mirror an id the agent already uses.
    """

    ext_id: str = Field(default="", max_length=64)
    #: The project this task belongs to, by its `ext_id` ("P3") or numeric id.
    #: Empty means a one-off, which plenty of work legitimately is.
    project: str = Field(default="", max_length=64)
    #: The task this is a subtask of, by `ext_id` ("T66") — same agent, same
    #: project (inherited when `project` is omitted), no cycles, at most three
    #: levels deep. A parent created earlier in the same batch may be named.
    parent: str = Field(default="", max_length=64)
    #: `work` (the default: the agent's own work) or `idea` — a person's want
    #: about the product, which must carry their words in `human_why`. Never
    #: inferred; the work toward an idea is its subtasks (`parent`).
    kind: Literal["work", "idea"] = "work"
    #: The task's own Drive folder (inside its project's), when it has one.
    drive_folder_id: str = Field(default="", max_length=128)
    drive_folder_url: str = Field(default="", max_length=500)
    title: str = Field(min_length=1, max_length=300)
    #: The people's own words behind the task, verbatim, with who and where.
    #: Separate from `rationale` (the agent's inference) and shown above it.
    #: Leave it empty rather than paraphrase.
    human_why: list[HumanWhyIn] = Field(default_factory=list, max_length=20)
    next_action: str = Field(default="", max_length=300)
    status: str = "suggested"  # normalized server-side
    owner: str = Field(default="", max_length=120)
    assigned: str = Field(default="", max_length=120)
    #: Who the next step waits on, by email — a member of the agent's workspace.
    waiting_on_email: str = Field(default="", max_length=254)
    confidence: str = Field(default="", max_length=10)
    score: str = Field(default="", max_length=8)
    review: str = ""
    rationale: str = ""
    source_url: str = Field(default="", max_length=500)
    plan: str = ""
    due: dt.date | None = None
    links: list[AgentTaskLink] = Field(default_factory=list)
    notes: str = ""
    position: int = 0
    #: What the task asks a person, if anything: `review` ("should I do
    #: this?") or `question` ("I need an answer"). Blank asks nothing.
    ask_kind: Literal["", "review", "question"] = ""
    ask_body: str = ""
    #: The turns that run when the task is approved (or its question answered).
    on_approve: list[TurnSpecIn] = Field(default_factory=list)
    batch_key: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=128)
    #: One of `AgentTask.POSTABLE_ORIGINS`; a retired spelling is normalized.
    origin: str = Field(default="", max_length=32)
    origin_ref: dict = Field(default_factory=dict)
    #: The turn that raised this task, if any.
    raised_by: uuid.UUID | None = None
    #: Free-text producer tag (the sheet / tool the task was mirrored from).
    source: str = Field(default="", max_length=100)

    @field_validator("origin")
    @classmethod
    def _known_origin(cls, v: str) -> str:
        # A retired spelling (cron, manual …) is accepted and normalized, as on
        # every other input carrying an origin.
        if v not in AgentTask.POSTABLE_ORIGINS and normalize_origin(v) == v:
            raise ValueError(f"origin must be one of {', '.join(o for o in AgentTask.POSTABLE_ORIGINS if o)}"
                             f" (or blank), got {v!r}")
        return normalize_origin(v)


class AgentTaskOut(StrictModel):
    """A task, addressed by `(agent_slug, ext_id)` — there is no other id."""

    agent_slug: str
    #: The workspace the task lives in (its agent's) — see `AgentProjectOut.workspace`.
    workspace: str = ""
    ext_id: str
    project_ext_id: str | None = None
    project_name: str | None = None
    #: The task this is a subtask of; null for a top-level task.
    parent_ext_id: str | None = None
    #: `idea` — a person's want, in their words (`human_why`); `work` otherwise.
    kind: Literal["work", "idea"] = "work"
    drive_folder_id: str = ""
    drive_folder_url: str = ""
    title: str
    #: What people actually said, verbatim, oldest first — above `rationale`.
    human_why: list[HumanWhyOut] = Field(default_factory=list)
    next_action: str
    status: Literal["suggested", "in_progress", "done", "declined"]
    owner: str
    assigned: str
    waiting_on_email: str | None = None
    # The ask, where the task carries one. Blank `ask_kind` means the task asks
    # nothing and is simply work in flight. Open until an action closes it.
    ask_kind: str = ""
    ask_body: str = ""
    ask_open: bool = Field(default=False,
                           validation_alias=AliasChoices("ask_is_open", "ask_open"))
    ask_closed_at: dt.datetime | None = None
    on_approve: list[dict] = Field(default_factory=list)
    dispatched_at: dt.datetime | None = None
    batch_key: str = ""
    #: The producer's key, echoed so an agent can match the tasks it published.
    #: "" when none was sent (the column is NULL then).
    idempotency_key: str = ""
    origin: str = ""
    confidence: str
    score: str
    review: str
    rationale: str
    source_url: str
    plan: str
    due: dt.date | None = None
    links: list[AgentTaskLink] = Field(default_factory=list)
    notes: str
    position: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _null_key_is_blank(cls, v):
        return v or ""


class AgentTaskPatch(StrictModel):
    """Partial update — only the fields sent are written."""

    #: `""` takes the task OUT of its project; omitting it leaves the task where
    #: it is. The two must differ, or patching a title would silently unfile it.
    project: str | None = Field(default=None, max_length=64)
    #: The parent task's `ext_id`; `""` makes it top-level again. Moving a
    #: parent's project moves its subtasks with it.
    parent: str | None = Field(default=None, max_length=64)
    #: `idea` needs at least one `human_why` quote (sent here or already on it).
    kind: Literal["work", "idea"] | None = None
    drive_folder_id: str | None = Field(default=None, max_length=128)
    drive_folder_url: str | None = Field(default=None, max_length=500)
    #: Who the next step waits on, by email. `""` clears it. A person canopy
    #: does not know is refused rather than silently dropped — the free-text
    #: `assigned` is where an unknown counterpart belongs, and a wait that
    #: looks routed but reaches nobody is the failure this field exists to end.
    waiting_on_email: str | None = Field(default=None, max_length=254)
    title: str | None = Field(default=None, max_length=300)
    #: REPLACES the list (send every quote you want kept); `[]` clears it.
    human_why: list[HumanWhyIn] | None = Field(default=None, max_length=20)
    next_action: str | None = Field(default=None, max_length=300)
    status: str | None = None
    owner: str | None = Field(default=None, max_length=120)
    assigned: str | None = Field(default=None, max_length=120)
    confidence: str | None = Field(default=None, max_length=10)
    score: str | None = Field(default=None, max_length=8)
    review: str | None = None
    rationale: str | None = None
    source_url: str | None = Field(default=None, max_length=500)
    plan: str | None = None
    due: dt.date | None = None
    notes: str | None = None
    position: int | None = None
    links: list[AgentTaskLink] | None = None


# ---- actions: everything a person does TO a task ----
class AgentTaskActionIn(StrictModel):
    #: approve · decline · reply (a reply needs a comment) · nudge · done.
    #: Field changes are a PATCH, not an action.
    action: Literal["approve", "decline", "reply", "nudge", "done"]
    comment: str = ""


class AgentTaskActionOut(StrictModel):
    id: int
    agent_slug: str
    task_ext_id: str
    action: str
    comment: str
    by: str
    #: `pending` until the agent has carried it out (its queue), then `applied`.
    status: str
    applied_at: dt.datetime | None = None
    result_note: str
    created_at: dt.datetime


class AgentTaskDetailOut(AgentTaskOut):
    #: Everything done to the task, newest first — "who approved this and why"
    #: is the closing row.
    actions: list[AgentTaskActionOut] = Field(default_factory=list)


class ActOut(StrictModel):
    task: AgentTaskOut
    action: AgentTaskActionOut
    #: Turns the action started (an approve or answer running `on_approve`).
    turn_ids: list[uuid.UUID] = Field(default_factory=list)


class ActionAppliedIn(StrictModel):
    result_note: str = ""


class TurnBriefOut(StrictModel):
    id: uuid.UUID
    status: str
    #: The turn's reported title if it has one, else the first 200 characters of
    #: its prompt. When the caller may not read the turn's content
    #: (`turn_access`), the prompt is blanked, so this is the title or empty.
    prompt_preview: str = ""
    created_at: dt.datetime
    task_ext_ids: list[str] = Field(default_factory=list)


class ProjectParticipantOut(StrictModel):
    """A person taking part in the project (fleet brain v1.1)."""

    id: int = Field(description="The person's id — `GET /api/people/{id}/?workspace=`.")
    display_name: str
    email: str = ""
    role: str = Field(default="", description="Free text: the part they play. Blank when canopy linked them itself.")
    source: Literal["fact", "turn", "manual", "summary", "review", "backing", "reply", "checkin"] = Field(
        description="How canopy first learned it: a fact filed against the project, a conversation "
                    "that raised one of its tasks, a person adding them, an agent writing their "
                    "contributor page, a storyboard comment, backing it, or replying on one of its tasks.")
    since: dt.datetime


# ---- proposed projects: the decision, the backings (apps/agents/proposals.py) ----

BackingStance = Literal["backs", "backs_with_changes", "passes"]


class ProjectBackerOut(StrictModel):
    kind: Literal["person", "agent"]
    #: The agent's slug when an agent backs; blank for a person.
    agent: str = ""
    person_id: int | None = None
    name: str
    email: str = ""


class ProjectBackingOut(StrictModel):
    backer: ProjectBackerOut
    stance: BackingStance
    line: str = ""
    #: Where it was given: web, mcp, slack, email — or huddle, a co-sign a huddle filed.
    source: str = "web"
    huddle_id: str = ""
    #: They backed an earlier version: the project has been revised since.
    stale: bool = False
    #: The reader's own backing.
    mine: bool = False
    updated_at: dt.datetime


class ProjectBackingIn(StrictModel):
    stance: BackingStance
    #: One line, required unless the stance is `backs`: what to change, or why you pass.
    line: str = Field(default="", max_length=280)


class AgentBackingIn(ProjectBackingIn):
    #: The huddle whose round-3 answer this co-sign records, if any.
    huddle_id: str = Field(default="", max_length=64)


class ProposalCommentOut(StrictModel):
    """A reply on the decision card — the project's comment thread."""

    by: str
    by_agent: bool = False
    comment: str
    at: dt.datetime


class ProposalPartnerOut(StrictModel):
    """A partner's linked task, on the partner's own board."""

    agent: str
    ext_id: str
    task_id: int
    status: str
    url: str


class ProposedProjectOut(StrictModel):
    """A proposed (or once-proposed) project as both the project page and the
    huddle page render it: what it is, its decision and whether the reader may
    make it, where people stand, what they said, and its partners."""

    id: int
    agent: str
    workspace: str
    ref: str
    name: str
    status: Literal["proposed", "active", "done", "archived"]
    outcome: str
    why: str = ""
    plan: list[str] = Field(default_factory=list)
    #: The project page (app-relative).
    url: str
    #: The page that proposed it — a huddle, a thread.
    source_url: str = ""
    proposed_at: dt.datetime | None = None
    #: When it is archived as "not taken up" if nobody starts or declines it.
    expires_at: dt.datetime | None = None
    owner_email: str | None = None
    #: The project's notes — where a decline's reason is kept.
    notes: str = ""
    #: The task it grew out of ("from <task> in <project>"), if it did.
    spawned_from: SpawnedFromOut | None = None
    #: The decision card ("Start this project?"): act on it with `POST …/tasks/{ref}/actions`.
    decision_task: str | None = None
    decision_task_id: int | None = None
    decision_open: bool = False
    #: The reader may approve or decline it (a workspace owner or admin, or the project's owner).
    may_decide: bool = False
    backings: list[ProjectBackingOut] = Field(default_factory=list)
    comments: list[ProposalCommentOut] = Field(default_factory=list)
    partners: list[ProposalPartnerOut] = Field(default_factory=list)


class ProjectProposeIn(StrictModel):
    """Create or revise a proposed project (`POST …/projects/propose/`)."""

    name: str = Field(min_length=1, max_length=200)
    #: What done looks like.
    outcome: str = Field(min_length=1)
    why: str = Field(min_length=1)
    #: The plan's steps, in order — each becomes a `suggested` task in the project.
    plan: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)
    #: Agent slugs that would work with the lead; each gets a linked task on its own board.
    partners: list[str] = Field(default_factory=list, max_length=10)
    #: The page that proposed it — a huddle, a thread.
    source_url: str = Field(default="", max_length=500)
    owner_email: str = Field(default="", max_length=254)
    #: Required to bring back a project of this name that was declined.
    new_evidence: str = ""
    #: The task this project grows out of, as `<agent>/<task ext_id>` — a check-in's
    #: proposed task that turned out to be a project's worth of work.
    from_task: str = Field(default="", max_length=130)
    #: The check-in this came from (its id), recorded on the `proposed` event.
    checkin: str = Field(default="", max_length=64)
    #: Deprecated (work groups are gone): accepted and ignored for one release.
    group: str = Field(default="", max_length=64)


class ProjectProposeOut(StrictModel):
    #: False when an open proposed project of that name was revised instead.
    created: bool
    project: ProposedProjectOut


class SpawnedProjectOut(StrictModel):
    """A project a task grew into: "spawned → <project>"."""

    agent: str
    ref: str
    name: str
    status: str
    url: str


class ProposedTaskOut(StrictModel):
    """A check-in's proposed task as both the project page and the huddle page
    render it: what it is, its decision (Start = approve, Decline = decline, the
    task's own actions) and whether the reader may make it, where people stand,
    what they said, its partners and any project it spawned."""

    id: int
    agent: str
    workspace: str
    #: The task's `ext_id` — act on it with `POST /api/agents/{agent}/tasks/{ref}/actions`.
    ref: str
    title: str
    status: Literal["suggested", "in_progress", "done", "declined"]
    why: str = ""
    done_when: str = ""
    plan: list[str] = Field(default_factory=list)
    project_ref: str = ""
    project_name: str = ""
    project_url: str = ""
    #: The page to open it on (its project's, app-relative).
    url: str
    #: The page that proposed it — a huddle.
    source_url: str = ""
    checkin: int | None = None
    run: int | None = None
    huddle_id: str = ""
    proposed_at: dt.datetime | None = None
    #: When it is declined as "not taken up" if nobody starts or declines it.
    expires_at: dt.datetime | None = None
    #: The task's notes — where a decline's reason is kept.
    notes: str = ""
    decision_open: bool = False
    #: The reader may start or decline it (the project's owner, the check-in's
    #: lead, or a workspace owner or admin).
    may_decide: bool = False
    backings: list[ProjectBackingOut] = Field(default_factory=list)
    comments: list[ProposalCommentOut] = Field(default_factory=list)
    partners: list[ProposalPartnerOut] = Field(default_factory=list)
    spawned: list[SpawnedProjectOut] = Field(default_factory=list)


class TaskProposeIn(StrictModel):
    """Create or revise a proposed task in a project (`POST …/projects/{ref}/tasks/propose/`)."""

    title: str = Field(min_length=1, max_length=300)
    why: str = Field(min_length=1)
    #: What done looks like.
    done_when: str = Field(min_length=1)
    #: The plan's steps, in order — each becomes a `suggested` child task.
    plan: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)
    #: Agent slugs that would work with the project's agent; each gets a linked task on its own board.
    partners: list[str] = Field(default_factory=list, max_length=10)
    #: The page that proposed it — a huddle.
    source_url: str = Field(default="", max_length=500)
    #: The check-in run it came from (its id), if any — it must be this project's.
    run: int | None = None
    #: Required to bring back a title that was declined in this project.
    new_evidence: str = ""


class TaskProposeOut(StrictModel):
    #: False when an open proposed task of that title was revised instead.
    created: bool
    task: ProposedTaskOut


class AgentProjectDetailOut(AgentProjectOut):
    #: The project's tasks, live ones first.
    tasks: list[AgentTaskOut] = Field(default_factory=list)
    #: The people the work involves, newest first (fleet brain v1.1).
    participants: list[ProjectParticipantOut] = Field(default_factory=list)
    #: The agent's latest turns that worked on any of those tasks.
    recent_turns: list[TurnBriefOut] = Field(default_factory=list)
    #: Set when the project was proposed (it has a decision card): the card the
    #: page renders, the same one the huddle page shows.
    proposal: ProposedProjectOut | None = None
    #: The tasks its check-ins proposed that wait for Start or Decline, each as
    #: the huddle page shows it.
    proposed_tasks: list[ProposedTaskOut] = Field(default_factory=list)


# ---- shared ----
class GoogleMintStartOut(StrictModel):
    """Where to send the browser to consent. Declared rather than left implicit:
    an undeclared response reaches the typed client as `undefined`, and the only
    way to consume it is a cast — which silently survives the route changing."""

    url: str


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


class AgentVaultIn(StrictModel):
    """Non-clobbering, like every other credential write here: a blank/omitted
    service_key leaves the stored one alone, so editing the vault name does not
    silently wipe the key."""

    vault: str | None = None
    service_key: str | None = None


class AgentVaultOut(StrictModel):
    vault: str = ""
    key_set: bool = False
    # How many declared refs canopy-web can actually LOCATE. Without this an
    # import that returns "45 skipped" is unexplainable from the UI: the cause is
    # always that runtime.yaml's source map never reached this deployment, and
    # nothing on the screen could say so.
    declared: int = 0
    locatable: int = 0


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

    `declared` distinguishes a ref the agent's runtime.yaml asks for from an
    orphan left behind when one was removed; `source` says which store a live
    value came from, so a canopy-web/1Password divergence during migration is
    visible rather than silent."""

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
    """401 from resolve: the agent requires a runner device proof (DPoP) and
    the request carried none that verified against an active key."""

    error: Literal["device_proof_required"]
    #: `not_routed`: the proof is an approved box's, but that box does not run
    #: this agent (no alert; fix the routing or stop asking). `no_valid_proof`:
    #: no proof, or one no active key matches (the owner is alerted).
    reason: Literal["not_routed", "no_valid_proof"] = "no_valid_proof"
    detail: str = ""


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
    #: The owner was emailed or pushed (false: delivery is off, or it failed).
    notified: bool = False
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


class DeviceBindingIn(StrictModel):
    """Require a device proof on resolve (only meaningful with credential_source
    canopy-web). Owner/admin only, like CredentialSourceIn."""

    require_device_binding: bool


class AgentCredentialRevealOut(StrictModel):
    """PLAINTEXT, once, to an agent admin at a browser who just confirmed a passkey."""

    name: str
    value: str


class AgentCredentialAccessOut(StrictModel):
    """Whether the CALLER may resolve this agent's secrets — never the values.

    The cheap check `canopy cred` makes before deciding how to resolve
    (canopy#850). `may_resolve` mirrors GET /credentials/resolve exactly, minus
    its bearer requirement (that is a property of the request, not of the
    caller): `via` is `runner` when the caller pairs a live runner this agent
    routes to, `admin` when they are the agent's owner or an admin, and null
    when neither holds — then `reason` names the access to ask for."""

    agent: str
    credential_source: Literal["1password", "canopy-web"]
    may_resolve: bool
    via: Literal["runner", "admin"] | None = None
    reason: str = ""


class AgentCredentialsResolveOut(StrictModel):
    """PLAINTEXT, for a runner. The one route that returns values.

    It carries the 1Password vault + service token as well, because the runner is
    what resolves this agent's secrets — canopy-web only custodies the key. Both
    ride this route rather than a new one so there is exactly ONE plaintext gate
    to reason about, and one audit entry per fetch."""

    values: dict[str, str] = Field(default_factory=dict)
    # Which vault this agent's secrets live in. The runner used to DERIVE this as
    # Agent-<Slug> in bash, which is fine until an agent's vault is named
    # anything else and silently resolves nothing.
    op_vault: str = ""
    # Scoped to that vault. Falls back on the box to the runner-wide token when
    # empty, so an agent with no key of its own keeps working exactly as before.
    op_sa_token: str = ""
    # The TENANT's shared vault + its own scoped token. A per-agent key reads
    # Agent-<Slug> and nothing else — by design — so the shared gog OAuth clients
    # were unreachable from inside a bootstrap pass that had already swapped to
    # one. Measured 2026-09-07: ACE imported a browser-minted token bound to
    # `canopy-web`, then could not read op://Canopy-Shared/gog-oauth-client-web
    # to get the client id+secret that token is useless without.
    #
    # Rides this route rather than a new one for the reason above: ONE plaintext
    # gate, one audit entry per fetch. Both blank on a tenant that has not set
    # them, and the box then behaves exactly as it does today.
    shared_op_vault: str = ""
    shared_op_sa_token: str = ""
    # The agent OWNER's GitHub token for this agent (`AgentDelegation`), so a box
    # can clone the agent's private repo and its private plugin repos. "" when
    # the owner has lent none. A turn does NOT read it from here: it asks for
    # its own, bound to the turn it claimed
    # (`POST /api/harness/runners/{id}/turns/{id}/github-token`).
    github_token: str = ""
    # The chrome-sales `.sf-creds.json` this agent BORROWS (an `AgentDelegation`
    # of service salesforce: the lender's own credential, resolved now, not a
    # copy). "" when it borrows none. The box writes it to
    # ~/.canopy/delegated/<slug>/chrome-sales/.sf-creds.json, the only place
    # chrome-sales reads Salesforce creds in an agent session.
    salesforce_creds: str = ""
    # THIS instance's mailbox (`Agent.email`), which the box sets Gmail up for.
    # Not a secret; it rides here because bootstrap already asks this route per
    # agent. It is the instance's, never the repo's — every instance of an
    # agent shares its repo, and deriving the address from the repo or the slug
    # would point two instances at one inbox (canopy-web#984).
    mailbox: str = ""
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


class ZkWrapMissingOut(StrictModel):
    """409 from resolve: the agent is zero-knowledge and the proving device
    holds no vault-key wrap at the current kv (its owner is alerted)."""

    error: Literal["zk_wrap_missing"]
    kv: int
    device_key: str


# ---- zero-knowledge vault (canopy-web#1550; apps/agents/zk.py) ----------------

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


class ZkValuesOut(StrictModel):
    kv: int
    names: list[str]


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
    #: Did the agent's secrets materialize (`op inject` of its .env.tpl)? None
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


# ---- skill history (pulled from the agent's repo) ----
class SkillHistoryGroupOut(StrictModel):
    title: str
    kind: Literal["phase", "agent", "none"]
    num: str
    skills: list[str]


class SkillHistoryCommitOut(StrictModel):
    sha: str
    date: str
    subject: str


class SkillHistorySkillOut(StrictModel):
    name: str
    # Each revision is [commit_index, lines_after, added, deleted]; commit_index
    # indexes SkillHistoryOut.commits. Tuples rather than objects because ACE
    # has ~2,300 of them and the page downloads them all.
    revisions: list[list[int]]


class SkillHistoryOut(StrictModel):
    agent: str
    repo_url: str
    head_sha: str
    synced_at: dt.datetime | None
    synced_with: str
    last_error: str
    credential_state: Literal["ok", "no_repo", "no_owner", "owner_not_connected", "repo_not_granted"]
    # The agent owner's display name (or email), "" when there is no owner.
    # The credential is the owner's, so the page names who has to act.
    owner_name: str
    # Whether the caller is the owner (only they can connect the GitHub grant).
    viewer_is_owner: bool
    # Whether the caller may force a sync (editor or owner in the workspace).
    viewer_can_sync: bool
    # GitHub's installation screen for the canopy-agents App, "" when the
    # deployment has no App configured.
    install_url: str
    groups: list[SkillHistoryGroupOut]
    checks: dict[str, str]
    present: list[str]
    commits: list[SkillHistoryCommitOut]
    skills: list[SkillHistorySkillOut]


# ---- GitHub: the owner's identity, lent to one agent (AgentDelegation) ----
class AgentSalesforceIn(StrictModel):
    #: The agent whose own Salesforce credential to borrow (e.g. "eva").
    lender: str


class AgentSalesforceOut(StrictModel):
    """Whose Salesforce identity this agent borrows, and who borrows its own.
    Never the credential."""

    set: bool = False
    owner_email: str = ""
    lender: str = ""
    #: The Salesforce user the borrowed credential acts as, from the last probe.
    username: str = ""
    error: str = ""
    checked_at: datetime | None = None
    updated_at: datetime | None = None
    #: Agents borrowing THIS agent's Salesforce credential.
    lent_to: list[str] = Field(default_factory=list)


class AgentGitHubIn(StrictModel):
    token: str


class AgentGitHubCheckOut(StrictModel):
    repo: str
    ok: bool
    detail: str = ""


class AgentGitHubOut(StrictModel):
    """What the settings screen shows about the owner's GitHub delegation to
    this agent. Never the token."""

    #: `owner/repo`, parsed from the agent's repo URL; "" when it is not GitHub.
    repo: str = ""
    owner_email: str = ""
    #: GitHub's new-token form, pre-filled for this agent.
    create_url: str = ""
    set: bool = False
    #: The GitHub account the token acts as, and the name commits carry.
    login: str = ""
    name: str = ""
    expires_at: datetime | None = None
    expired: bool = False
    expiring_soon: bool = False
    checks: list[AgentGitHubCheckOut] = Field(default_factory=list)
    #: Why the last check could not run at all (GitHub rejected the token).
    error: str = ""
    checked_at: datetime | None = None
    updated_at: datetime | None = None
    #: Repos whose agentless project turns (`Turn.project`, no agent) run as
    #: this agent's GitHub identity — its `RepoIdentity` rows. Read-only here;
    #: set in Django admin.
    identity_for_repos: list[str] = Field(default_factory=list)


# ---- A2A Agent Card (apps/agents/agent_card.py) ----
#
# The Agent2Agent (A2A) protocol v1.0 AgentCard, field for field from the
# normative `lf.a2a.v1` proto (specification/a2a.proto). A2A's JSON form is
# camelCase (spec §5.5), so each model generates camelCase aliases and the
# routes serialize `by_alias`; optional fields that are unset are OMITTED
# (`exclude_none`), which is also what §8.4.1's canonicalization requires the
# day these cards are signed. Output-only: nothing parses a card from a client.

class _A2AModel(StrictModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True,
                              extra="forbid", from_attributes=True)


class A2AAgentProvider(_A2AModel):
    organization: str
    url: str


class A2AAgentCapabilities(_A2AModel):
    streaming: bool = False
    push_notifications: bool = False
    extended_agent_card: bool = False


class A2AAgentInterface(_A2AModel):
    url: str
    protocol_binding: str
    protocol_version: str


class A2AStringList(_A2AModel):
    # The proto field is `list`; named otherwise here so it cannot shadow the
    # builtin inside the class body.
    values: list[str] = Field(default_factory=list, alias="list")


class A2ASecurityRequirement(_A2AModel):
    # A scheme NAME (a key of `securitySchemes`) → the scopes it needs.
    schemes: dict[str, A2AStringList]


class A2AHTTPAuthSecurityScheme(_A2AModel):
    description: str = ""
    scheme: str
    bearer_format: str = ""


class A2AAPIKeySecurityScheme(_A2AModel):
    description: str = ""
    location: Literal["query", "header", "cookie"]
    name: str


class A2ASecurityScheme(_A2AModel):
    # A proto `oneof`: exactly one of these is set on any one scheme.
    http_auth_security_scheme: A2AHTTPAuthSecurityScheme | None = None
    api_key_security_scheme: A2AAPIKeySecurityScheme | None = None


class A2AAgentSkill(_A2AModel):
    id: str
    name: str
    description: str
    tags: list[str]
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None
    security_requirements: list[A2ASecurityRequirement] | None = None


class A2AAgentCardOut(_A2AModel):
    """An A2A v1.0 Agent Card, generated from the agent's declared interface."""

    name: str
    description: str
    supported_interfaces: list[A2AAgentInterface]
    provider: A2AAgentProvider
    version: str
    documentation_url: str | None = None
    capabilities: A2AAgentCapabilities
    security_schemes: dict[str, A2ASecurityScheme]
    security_requirements: list[A2ASecurityRequirement]
    default_input_modes: list[str]
    default_output_modes: list[str]
    skills: list[A2AAgentSkill]
    icon_url: str | None = None


# ---- ideas + contributor pages (hal T69) ----


def _http_url(value: str) -> str:
    value = (value or "").strip()
    if value and not value.startswith(("https://", "http://")):
        raise ValueError("must be an http(s) URL")
    return value


class ContributorQuoteIn(StrictModel):
    """Something the person said, verbatim, and where they said it."""

    quote: str = Field(min_length=1, max_length=600, description="Their words, verbatim. Trim, never rephrase.")
    source_url: str = Field(min_length=1, max_length=500,
                            description="Where they said it: a canopy session, a doc comment, an email thread.")
    said_at: dt.datetime | None = None
    where: str = Field(default="", max_length=120,
                       description="A short label for the link, e.g. 'Supply session, Sep 10'.")

    _url = field_validator("source_url")(_http_url)


class ContributorIdeaIn(StrictModel):
    """One of the project's ideas (a `kind: "idea"` task) the agent read as this
    person's, with their words. The page lists the ideas carrying the person's
    own words (their human-why); this is the agent's reading beside it."""

    task: str = Field(min_length=1, max_length=64, description="The idea task's ext_id on this project (T71).")
    quote: str = Field(default="", max_length=600)
    source_url: str = Field(default="", max_length=500)
    said_at: dt.datetime | None = None

    _url = field_validator("source_url")(_http_url)


class ContributorOpenIn(StrictModel):
    """Something they want that no task covers yet."""

    text: str = Field(min_length=1, max_length=300, description="A short plain statement of the want.")
    quote: str = Field(default="", max_length=600)
    source_url: str = Field(default="", max_length=500)
    said_at: dt.datetime | None = None

    _url = field_validator("source_url")(_http_url)


class ContributorSourceIn(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)

    _url = field_validator("url")(_http_url)


class ContributorSummaryIn(StrictModel):
    """A contributor page, as the agent that read the person's sessions writes it.

    `goal` is the one place the agent's own words appear — keep it short and
    built from theirs; everything else is their words with a link."""

    goal: str = Field(min_length=1, max_length=800,
                      description="What they are trying to achieve on this project, in a few sentences, "
                                  "built from their own words.")
    quotes: list[ContributorQuoteIn] = Field(min_length=1, max_length=12)
    ideas: list[ContributorIdeaIn] = Field(default_factory=list, max_length=60)
    open: list[ContributorOpenIn] = Field(default_factory=list, max_length=20)
    sources: list[ContributorSourceIn] = Field(default_factory=list, max_length=10,
                                               description="What the agent read to write this.")
    basis: str = Field(default="", max_length=300,
                       description="How much it rests on, e.g. '458 prompts across 22 sessions, Sep 10 to Oct 9'.")


class IdeaWhoOut(StrictModel):
    person_id: int | None = Field(description="Null when canopy does not know the person here.")
    name: str


class IdeaTaskOut(StrictModel):
    ext_id: str
    title: str
    status: str


class IdeaOut(StrictModel):
    """An idea: a task with `kind: "idea"` — a person's want about the product,
    in their words, with the work toward it as its subtasks."""

    ext_id: str = Field(description="The idea task's ext_id.")
    title: str
    status: str = Field(description="The idea task's status.")
    stage: Literal["proposed", "started", "shipped", "declined"] = Field(
        description="Its status in a contributor's words: suggested → proposed, in progress → "
                    "started, done → shipped (marked done by its owner or its person), declined.")
    quote: str = Field(description="Their own words — the idea's first human-why.")
    who: IdeaWhoOut
    source_url: str = ""
    said_at: dt.datetime | None = None
    people: list[int] = Field(description="Everyone whose words it carries (Person ids).")
    subtasks: list[IdeaTaskOut] = Field(description="The work toward it.")
    created_at: dt.datetime
    updated_at: dt.datetime


class IdeaIn(StrictModel):
    """Your own idea, in your own words. Any member may. To record someone
    else's words, create a task with `kind: "idea"` and `human_why`."""

    quote: str = Field(min_length=1, max_length=2000, description="Your words, verbatim.")
    title: str = Field(default="", max_length=300)


class IdeaCreatedOut(StrictModel):
    idea: IdeaOut
    notified: list[str] = Field(description="Who was told about it, by name.")


class StageCountsOut(StrictModel):
    proposed: int = 0
    started: int = 0
    shipped: int = 0
    declined: int = 0


class ContributorBriefOut(StrictModel):
    id: int
    display_name: str
    role: str = ""
    source: Literal["fact", "turn", "manual", "summary", "review", "backing", "reply", "idea"] = Field(
        description="How they joined (a participant row's source), or `idea`: not a participant, "
                    "but an idea's own evidence (its human why, or the turn that raised it) names them.")
    since: dt.datetime
    has_page: bool
    page_withheld: bool = Field(description="A page exists but the reader (an agent) may not use it.")
    goal: str = ""
    written_at: dt.datetime | None = None
    ideas: StageCountsOut


class ProjectIdeasOut(StrictModel):
    agent: str
    project: str
    name: str
    stages: StageCountsOut
    ideas: list[IdeaOut]
    contributors: list[ContributorBriefOut]


class WaitingLineOut(StrictModel):
    text: str = Field(description="One line.")
    url: str = ""
    waiting_on: str = Field(description="Who it waits on, by name.")


class ProjectTeamOut(StrictModel):
    """A project as a teammate sees it: what it is, where it stands, the ideas,
    the people. None of the agent's process — no task cards or numbers."""

    agent: str
    agent_name: str
    project: str
    name: str
    outcome: str
    status: str
    owner_name: str
    links: list[AgentTaskLink]
    may_edit: bool = Field(description="May the caller edit the project's work (the work board is theirs).")
    my_person_id: int | None = Field(default=None, description="The caller as a Person: they may mark their own ideas shipped.")
    built: list[IdeaOut] = Field(description="Ideas marked shipped.")
    in_progress: list[IdeaOut] = Field(description="Ideas a task has started on.")
    waiting: list[WaitingLineOut] = Field(description="Open decisions waiting on people, one line each.")
    stages: StageCountsOut
    ideas: list[IdeaOut]
    contributors: list[ContributorBriefOut]


class ContributorQuoteOut(StrictModel):
    quote: str
    source_url: str
    said_at: dt.datetime | None = None
    where: str = ""


class ContributorOpenOut(StrictModel):
    text: str
    quote: str = ""
    source_url: str = ""
    said_at: dt.datetime | None = None


class ContributorSummaryOut(StrictModel):
    goal: str
    quotes: list[ContributorQuoteOut]
    open: list[ContributorOpenOut]
    sources: list[AgentTaskLink]
    basis: str = ""
    written_at: dt.datetime | None = None
    written_by: str = ""


class ContributorPersonOut(StrictModel):
    id: int
    display_name: str
    email: str = ""


class ContributorPageOut(StrictModel):
    person: ContributorPersonOut
    agent: str
    project: str
    project_name: str
    role: str = ""
    participant_since: dt.datetime | None = None
    summary_state: Literal["written", "not_written", "withheld"]
    summary: ContributorSummaryOut | None = None
    ideas: list[IdeaOut]
    stages: StageCountsOut
    may_write: bool = Field(description="May the caller write this page (an agent with the person's "
                                        "permission, or the person themself)?")
    why_not: str = ""


# ---- engagement: what people do with projects and huddles ---------------------


class EngagementViewIn(StrictModel):
    """The page a person opened: a project (its numeric `id`) OR a huddle."""

    project: int | None = None
    huddle_id: str = Field(default="", max_length=64)


class EngagementViewOut(StrictModel):
    #: False when this person's view of this object was already counted today (UTC).
    recorded: bool


class EngagementCountOut(StrictModel):
    kind: str
    surface: str
    n: int


class EngagementPersonOut(StrictModel):
    user_id: int
    email: str = ""
    #: Everything they did in the window.
    events: int


class EngagementProposalOut(StrictModel):
    agent: str
    ref: str
    name: str
    status: str
    proposed_at: dt.datetime
    #: Seconds from proposing to the first person backing, commenting or deciding; null when nobody did.
    first_reaction_s: float | None = None
    #: approved / declined / expired, or null while it is still open.
    decision: str | None = None
    decision_s: float | None = None
    backers: int = 0
    comments: int = 0
    #: For a proposed task: its project's ext_id.
    project: str = ""


class EngagementRunsOut(StrictModel):
    announced: int = 0
    confirmed: int = 0
    skipped: int = 0
    expired: int = 0
    #: Median seconds from a run's announcement to its lead's Confirm.
    median_confirm_s: float | None = None


class EngagementSummaryOut(StrictModel):
    since: dt.datetime
    days: int
    counts: list[EngagementCountOut] = Field(default_factory=list)
    people: list[EngagementPersonOut] = Field(default_factory=list)
    proposals: list[EngagementProposalOut] = Field(default_factory=list)
    #: Proposed projects no person reacted to at all.
    unanswered: list[EngagementProposalOut] = Field(default_factory=list)
    #: Tasks a check-in proposed in the window — `ref` is the task's, `project` its project's.
    task_proposals: list[EngagementProposalOut] = Field(default_factory=list)
    unanswered_tasks: list[EngagementProposalOut] = Field(default_factory=list)
    runs: EngagementRunsOut
