"""Pydantic schemas for the /api/agents surface."""
from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Literal

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
    # agent MOVES it (caller must be a member of the target). Empty → legacy
    # behavior (default workspace for unhomed agents).
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


class RunnerPreferenceIn(StrictModel):
    """Set an agent's ordered runner-kind preference (the runner-order UI)."""

    runner_preference: list[str] = Field(default_factory=list)


class TurnModeIn(StrictModel):
    """Flip an agent's turn mode — a human decision, made from the board.

    Deliberately its own endpoint (not part of AgentIn): the agent-repo
    self-publish upsert must never be able to change its own autonomy."""

    turn_mode: Literal["manual", "auto"]


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
    """What an agent's "everything else" runs on. `own` True: its own list
    (`GET /runners`). Otherwise it follows `workspace`'s default order —
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
    turn_count: int = 0
    latest_sync_at: dt.datetime | None = None
    latest_turn_at: dt.datetime | None = None


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


class AgentCanopyUserIn(StrictModel):
    # None unlinks.
    user_id: int | None


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


class AgentProjectIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    ext_id: str = Field(default="", max_length=64)
    outcome: str = ""
    status: str = "active"
    owner_note: str = Field(default="", max_length=200)
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
    drive_folder_id: str | None = Field(default=None, max_length=128)
    drive_folder_url: str | None = Field(default=None, max_length=500)
    repo_slug: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    links: list[AgentTaskLink] | None = None


class AgentProjectOut(StrictModel):
    id: int
    agent_slug: str
    ext_id: str
    name: str
    outcome: str
    status: Literal["active", "done", "archived"]
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
    title: str = Field(min_length=1, max_length=300)
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
    ext_id: str
    project_ext_id: str | None = None
    project_name: str | None = None
    title: str
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


class AgentTaskPatch(StrictModel):
    """Partial update — only the fields sent are written."""

    #: `""` takes the task OUT of its project; omitting it leaves the task where
    #: it is. The two must differ, or patching a title would silently unfile it.
    project: str | None = Field(default=None, max_length=64)
    #: Who the next step waits on, by email. `""` clears it. A person canopy
    #: does not know is refused rather than silently dropped — the free-text
    #: `assigned` is where an unknown counterpart belongs, and a wait that
    #: looks routed but reaches nobody is the failure this field exists to end.
    waiting_on_email: str | None = Field(default=None, max_length=254)
    title: str | None = Field(default=None, max_length=300)
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
    #: approve · decline · reply (a reply needs a comment) · dispatch · done.
    #: Field changes are a PATCH, not an action.
    action: Literal["approve", "decline", "reply", "dispatch", "done"]
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


class AgentProjectDetailOut(AgentProjectOut):
    #: The project's tasks, live ones first.
    tasks: list[AgentTaskOut] = Field(default_factory=list)
    #: The agent's latest turns that worked on any of those tasks.
    recent_turns: list[TurnBriefOut] = Field(default_factory=list)


# ---- shared ----
class GoogleMintStartOut(StrictModel):
    """Where to send the browser to consent. Declared rather than left implicit:
    an undeclared response reaches the typed client as `undefined`, and the only
    way to consume it is a cast — which silently survives the route changing."""

    url: str


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
    # THIS instance's mailbox (`Agent.email`), which the box sets Gmail up for.
    # Not a secret; it rides here because bootstrap already asks this route per
    # agent. It is the instance's, never the repo's — every instance of an
    # agent shares its repo, and deriving the address from the repo or the slug
    # would point two instances at one inbox (canopy-web#984).
    mailbox: str = ""


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
