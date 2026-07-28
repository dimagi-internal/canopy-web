"""Pydantic schemas for the /api/agents surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel
# framework→framework: agents and harness are both framework tier, and the
# source vocabulary has ONE definition (harness owns Turn.origin).
from apps.harness.schemas import RoutableSource


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
    # Ordered runner-kind preference, e.g. ["cloud","emdash"]. None = leave unchanged.
    runner_preference: list[str] | None = None


class RunnerPreferenceIn(StrictModel):
    """Set an agent's ordered runner-kind preference (the runner-order UI)."""

    runner_preference: list[str] = Field(default_factory=list)


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
    runner_id: uuid.UUID
    runner_name: str
    kind: str
    strict: bool
    online: bool
    ready: bool
    enabled: bool = True
    queued_count: int = 0


class AgentRunnerRuleIn(StrictModel):
    """One rule of the wholesale-replace body. `source` is typed as the routable
    literal so an unknown source is a 422 here rather than a rule that silently
    never matches anything — and so the generated TypeScript carries the union."""

    source: RoutableSource
    runner_id: uuid.UUID
    strict: bool = False
    enabled: bool = True


class AgentRunnerRulesIn(StrictModel):
    """Wholesale replace of an agent's source rules. Scoped to non-empty-source
    rows: the default ordered list is the sibling endpoint's business, and neither
    write may clobber the other's rows."""

    rules: list[AgentRunnerRuleIn] = Field(default_factory=list)


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


class AgentDetailOut(AgentOut):
    sync_count: int = 0
    work_product_count: int = 0
    skill_count: int = 0
    task_count: int = 0
    turn_count: int = 0
    latest_sync_at: dt.datetime | None = None
    latest_turn_at: dt.datetime | None = None


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


class AgentTurnOut(StrictModel):
    id: int
    agent_slug: str
    cli_session_id: str
    title: str
    summary: str
    task_ext_ids: list[str] = Field(default_factory=list)
    work_product_urls: list[str] = Field(default_factory=list)
    session_slug: str
    share_token: str
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    source: str
    created_at: dt.datetime


# ---- Work products ----
class AgentWorkProductIn(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="", max_length=40)
    url: str = Field(min_length=1, max_length=500)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = Field(default="", max_length=100)


class AgentWorkProductBatchIn(StrictModel):
    work_products: list[AgentWorkProductIn] = Field(min_length=1)


class AgentWorkProductOut(StrictModel):
    id: int
    agent_slug: str
    title: str
    kind: str
    url: str
    description: str
    tags: list[str] = Field(default_factory=list)
    source: str
    created_at: dt.datetime


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


# ---- tasks (source: a Google Sheet; rendered as a board) ----
class AgentTaskLink(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)


class AgentTaskIn(StrictModel):
    ext_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    next_action: str = Field(default="", max_length=300)
    status: str = "suggested"  # normalized server-side
    owner: str = Field(default="", max_length=120)
    assigned: str = Field(default="", max_length=120)
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
    source: str = Field(default="", max_length=100)


class AgentTaskSyncIn(StrictModel):
    """Full replacement of the agent's task board from the source sheet."""

    tasks: list[AgentTaskIn] = Field(default_factory=list)


class AgentTaskOut(StrictModel):
    id: int
    agent_slug: str
    ext_id: str
    title: str
    next_action: str
    status: Literal["suggested", "in_progress", "done", "declined"]
    owner: str
    assigned: str
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
    updated_at: dt.datetime


class AgentTaskPatch(StrictModel):
    """Partial update — only the fields sent are written."""

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


# ---- task commands (the board's action queue) ----
class AgentTaskCommandIn(StrictModel):
    kind: Literal["accept", "decline", "dispatch", "reassign", "edit", "comment", "done"]
    payload: dict = Field(default_factory=dict)  # reason / assignee / next_action / note
    created_by: str = Field(default="", max_length=200)


class AgentCommandApplyIn(StrictModel):
    result_note: str = ""


class AgentTaskCommandOut(StrictModel):
    id: int
    agent_slug: str
    task_id: int | None = None
    task_ext_id: str
    task_title: str
    kind: str
    payload: dict = Field(default_factory=dict)
    status: str
    created_by: str
    result_note: str
    created_at: dt.datetime
    applied_at: dt.datetime | None = None


class CommandResultOut(StrictModel):
    """Returned when the UI posts a command: the queued command + the (maybe
    immediately-updated) task."""

    command: AgentTaskCommandOut
    task: AgentTaskOut | None = None


# The supervisor inbox is now a pure query over harness.Item (open items across
# the fleet), served by apps/harness/items_api.py — no projection DTO lives here.


# ---- shared ----
class CountOut(StrictModel):
    created: int = 0
    replaced: int = 0
    count: int = 0
