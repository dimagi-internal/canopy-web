"""Pydantic schemas for the /api/harness surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from canopy_cron import validate_cron, validate_timezone
from ninja import Schema
from pydantic import Field, field_validator

# Kept in lockstep with Turn.ORIGIN_CHOICES / Turn.ROUTING_CHOICES (models.py).
# These are the values the DB columns accept (origin max_length=10, routing
# max_length=15); typing the INPUT schemas as Literals turns an out-of-set value
# into a 422 at the API boundary instead of a Postgres "value too long" 500 that
# SQLite CI can't reproduce. Output schemas stay `str` — they serialize values the
# DB already validated, and a Literal there would break on any legacy row.
Origin = Literal["board", "api", "slack", "cron", "manual", "email"]
Routing = Literal["prefer_local", "local_only", "any"]


class RunnerIn(Schema):
    name: str
    kind: str  # emdash|cloud|remote
    capabilities: dict = {}
    host: str = ""  # macOS user@hostname — load-bearing for session reuse across accounts
    workspace: str = ""  # tenant slug; defaults to the pairer's default workspace


class RunnerCapabilitiesIn(Schema):
    # Wholesale replacement, like the skill catalog — the caller sends the full
    # capabilities it wants (e.g. {"agents": [...], "projects": ["canopy-web"]}).
    capabilities: dict


class RunnerOut(Schema):
    id: uuid.UUID
    name: str
    kind: str
    status: str
    status_note: str
    ready: bool
    ready_note: str
    last_heartbeat_at: dt.datetime | None
    capabilities: dict
    host: str
    code_branch: str
    workspace: str | None
    # The human who paired the runner. This — NOT `workspace` — is what governs
    # what the runner may WORK FOR (claim_next_turn derives the tenant from the
    # pairer's workspace memberships, so a runner serves agents across every
    # workspace its pairer belongs to). `workspace` is only the home/visibility
    # tenant. Surfaced so the supervisor can show the meaningful owner instead of
    # implying a single-workspace serving scope.
    paired_by_email: str | None

    @staticmethod
    def resolve_workspace(obj) -> str | None:
        return obj.workspace_id

    @staticmethod
    def resolve_paired_by_email(obj) -> str | None:
        return obj.paired_by.email if obj.paired_by_id else None

    @staticmethod
    def resolve_status(obj) -> str:
        # Serve the derived value, not the stored column: heartbeat() writes
        # ONLINE and nothing ever demotes it, so the raw status lies once a
        # runner goes quiet. See Runner.live_status.
        return obj.live_status


class HeartbeatIn(Schema):
    active_turn_ids: list[str] = []
    degraded: bool = False
    note: str = ""
    host: str = ""  # refresh the owning macOS host (in case a runner row is reused)
    ready: bool = True   # can the runner fire a turn (cdp healthy ∧ not recently failed)
    ready_note: str = ""
    code_branch: str = ""  # the runner checkout's git branch — supervisor alerts on non-main


class ResolveSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the pairer)
    thread_key: str


class ResolveSessionOut(Schema):
    reuse: bool
    new_thread: bool
    emdash_task_id: str
    agent_task_ext_id: str
    summary: str
    link_id: str | None


class RecordSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the pairer)
    thread_key: str
    emdash_task_id: str = ""
    session_id: str = ""
    agent_task_ext_id: str | None = None
    summary: str | None = None


class ReportedSessionIn(Schema):
    emdash_task: str  # the emdash task NAME
    project: str = ""
    status: str = ""
    last_interacted_at: dt.datetime | None = None
    recent_messages: list = []  # Phase B populates this; ignored/empty in Phase A


class ReportSessionsIn(Schema):
    sessions: list[ReportedSessionIn] = []


class EmdashSessionOut(Schema):
    id: uuid.UUID
    emdash_task: str
    project: str
    status: str
    last_interacted_at: dt.datetime | None
    recent_messages: list
    workspace: str
    runner_name: str

    @staticmethod
    def resolve_workspace(obj) -> str:
        return obj.workspace_id

    @staticmethod
    def resolve_runner_name(obj) -> str:
        return obj.runner_name


class SessionReportOut(Schema):
    """Result of a runner's wholesale session report (POST /runners/{id}/sessions).

    Named distinctly from apps.agents.schemas.CountOut ({created, replaced, count}) —
    Django Ninja keys OpenAPI components by class title, so two Pydantic models both
    named CountOut collapse into one component and one silently wins, dropping fields
    from the other's advertised schema."""

    count: int


class TurnIn(Schema):
    # Exactly one of agent_slug / project. Enforced in the view (422) rather than
    # by a validator so the error matches the rest of the harness's shape.
    agent_slug: str = ""
    project: str = ""
    origin: Origin
    idempotency_key: str
    prompt: str = ""
    origin_ref: dict = {}
    routing: Routing = "prefer_local"


class TurnOut(Schema):
    id: uuid.UUID
    # Exactly one of these is set — a turn targets an agent or a repo, never
    # both. Consumers should read `target` unless they specifically need to know
    # which kind it is.
    agent_slug: str | None
    project: str
    target: str
    # The tenant the runner must pass back to record/resolve a PROJECT session
    # link (the pairer may belong to several workspaces; the turn knows its own).
    # Derived: agent turns report their agent's workspace, project turns their own.
    workspace_slug: str | None
    origin: str
    status: str
    routing: str
    prompt: str
    origin_ref: dict
    claimed_by_name: str | None
    enqueued_by_email: str | None
    session_id: str
    result_note: str
    created_at: dt.datetime
    claimed_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    lease_expires_at: dt.datetime | None

    @staticmethod
    def resolve_agent_slug(obj) -> str | None:
        # None for project turns — dereferencing obj.agent unconditionally is
        # what this used to do, and it 500s the moment agent can be NULL.
        if obj.agent_id:
            return obj.agent.slug
        # A chat SESSION turn targets a Session, not an agent — but you chat WITH an
        # agent, so surface the session's agent as the emdash target the runner drives.
        cs = getattr(obj, "chat_session", None)
        return cs.agent.slug if cs and cs.agent_id else None

    @staticmethod
    def resolve_project(obj) -> str:
        # A project turn stores its repo on the column; a PROJECT chat session
        # carries it on the session (the Turn.project column stays empty — the
        # agent XOR project XOR session constraint forbids setting it there).
        if obj.project:
            return obj.project
        cs = getattr(obj, "chat_session", None)
        return cs.project if cs is not None else ""

    @staticmethod
    def resolve_workspace_slug(obj) -> str | None:
        # Agent turns derive tenancy via the agent; project turns store their own;
        # a session turn (agent-backed or project-backed) derives it from the session.
        if obj.agent_id:
            return obj.agent.workspace_id
        cs = getattr(obj, "chat_session", None)
        if cs is not None:
            return cs.workspace_id
        return obj.workspace_id

    @staticmethod
    def resolve_claimed_by_name(obj) -> str | None:
        return obj.claimed_by.name if obj.claimed_by else None

    @staticmethod
    def resolve_enqueued_by_email(obj) -> str | None:
        return obj.enqueued_by.email if obj.enqueued_by_id else None


class TurnEventIn(Schema):
    kind: str
    payload: dict = {}


class TurnEventsIn(Schema):
    events: list[TurnEventIn]


class TurnEventOut(Schema):
    seq: int
    ts: dt.datetime
    kind: str
    payload: dict


class TurnEventsOut(Schema):
    events: list[TurnEventOut]


class TurnEventCountOut(Schema):
    count: int


class TurnStartIn(Schema):
    session_id: str = ""


class TurnFinishIn(Schema):
    status: str  # done|failed
    result_note: str = ""


class ScheduleIn(Schema):
    """Create payload. Cron + tz validate here so a bad expression 422s as
    problem+json at edit time — a typo that silently never fires is the worst
    failure mode a scheduler has."""

    name: str
    prompt: str
    cron: str
    timezone: str = "UTC"
    enabled: bool = True
    routing: str = "prefer_local"
    grace_minutes: int = 120
    notify: list[str] = ["inbox"]

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)

    @field_validator("name", "prompt")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("must not be blank")
        return v.strip()


class SchedulePatch(Schema):
    """Partial update. Every field optional; the same validators apply to any
    field actually supplied."""

    name: str | None = None
    prompt: str | None = None
    cron: str | None = None
    timezone: str | None = None
    enabled: bool | None = None
    routing: str | None = None
    grace_minutes: int | None = None
    notify: list[str] | None = None

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v is not None else v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str | None) -> str | None:
        return validate_timezone(v) if v is not None else v


class ScheduleOut(Schema):
    id: int
    agent_slug: str
    name: str
    prompt: str
    cron: str
    timezone: str
    enabled: bool
    routing: str
    grace_minutes: int
    notify: list[str]
    last_slot: dt.datetime | None = None
    # The anchor the runner MUST pass as due_slot(after=...). Server-computed as
    # `last_slot or created_at` so the runner cannot get the fallback wrong.
    # Without it a fresh schedule (last_slot=None) fires once for the slot BEFORE
    # it existed — a schedule created Wednesday would immediately owe last
    # Friday's report. See the runner-side section.
    fire_after: dt.datetime
    next_runs: list[dt.datetime] = []
    last_status: str = ""
    created_by_email: str | None = None  # who set it up (null for pre-attribution rows)
    created_at: dt.datetime
    updated_at: dt.datetime


class ScheduledFireOut(Schema):
    schedule: ScheduleOut
    workspace_slug: str | None = None
    fires: list[dt.datetime]


class ScheduleWeekOut(Schema):
    start: dt.datetime
    items: list[ScheduledFireOut]


class SchedulePreviewIn(Schema):
    """Preview a cron the user is still typing — no row exists yet."""

    cron: str
    timezone: str = "UTC"

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str) -> str:
        return validate_cron(v)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_timezone(v)


class SchedulePreviewOut(Schema):
    next_runs: list[dt.datetime]


class ScheduleFireIn(Schema):
    """The runner's report that a slot came due. The server re-derives nothing —
    but the slot is only honored as an idempotency anchor, never as a claim of
    authority: tenant scoping gates the route."""

    slot: dt.datetime


# ---------------------------------------------------------------------------
# Runner streams — the poll-fallback sync + live-event fan-out (SP3 Task 4)
# ---------------------------------------------------------------------------


class StreamDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str


class StreamSyncOut(Schema):
    streams: list[StreamDescriptorOut] = []


class LiveEventIn(Schema):
    kind: str
    seq: int
    payload: dict = {}


class SessionStreamIn(Schema):
    session_id: uuid.UUID
    events: list[LiveEventIn] = []


class StreamPostOut(Schema):
    count: int


# ---------------------------------------------------------------------------
# On-demand backfill — the runner-facing half (Plan 3 Task 6)
# ---------------------------------------------------------------------------


class BackfillDescriptorOut(Schema):
    session_id: str
    session_key: str
    project: str


class BackfillSyncOut(Schema):
    backfills: list[BackfillDescriptorOut] = []


class BackfillMessageIn(Schema):
    role: str
    text: str = ""


class SessionBackfillIn(Schema):
    session_id: uuid.UUID
    messages: list[BackfillMessageIn] = []


class BackfillWriteOut(Schema):
    written: int


# ---------------------------------------------------------------------------
# Items — the supervisor's queue (the dual of Turn)
# ---------------------------------------------------------------------------


class TurnSpecIn(Schema):
    """One deferred Turn enqueue. `target_agent=""` means the item's own agent —
    self-dispatch is the default; Ada's fan-out is this field set."""

    prompt: str = ""
    target_agent: str = ""
    origin: Origin = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    routing: Routing = "prefer_local"


class ItemIn(Schema):
    # No `notify` kind: an FYI asks nothing of you, and that is the timeline.
    kind: Literal["review", "question"] = "review"
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    origin: Origin = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    dispatch: list[TurnSpecIn] = Field(default_factory=list)
    batch_key: str = ""
    idempotency_key: str = Field(min_length=1, max_length=128)
    raised_by: uuid.UUID | None = None


class ItemOut(Schema):
    id: uuid.UUID
    agent_slug: str
    # Echoed back so a producer can reconcile its batch against what landed, and so
    # the UI has a stable, human-readable key for test ids.
    idempotency_key: str
    kind: str
    title: str
    body: str
    origin: str
    origin_ref: dict[str, Any]
    state: str
    decision: str
    comment: str
    decided_by: str
    decided_by_email: str | None = None  # resolved from the User FK, string fallback
    decided_at: dt.datetime | None = None
    dispatch: list[dict[str, Any]]
    dispatched_at: dt.datetime | None = None
    batch_key: str
    created_at: dt.datetime


class ItemDecideIn(Schema):
    # CLOSED set — a generic inbox must render buttons for an item it has never
    # seen. "" is valid for a question, whose answer is the comment.
    decision: Literal["implement", "skip", "defer", ""] = ""
    comment: str = ""


class ItemDismissIn(Schema):
    # Dismiss carries an optional reason: a PRODUCER retracting its own item raised
    # in error (e.g. an agent that verified the friction was already fixed) records
    # WHY, so the board shows "retracted: already shipped" instead of a bare
    # dismissed row. Optional — an empty-body dismiss stays valid.
    comment: str = ""


# ---- Runner credentials (per-runner, cloud-only; laptop uses emdash) ----
class RunnerCredentialIn(Schema):
    """Set a cloud runner's credentials. A field left None is unchanged
    (non-clobbering) — update just the Claude token without wiping the rest."""

    claude_token: str | None = None
    github_token: str | None = None
    op_sa_token: str | None = None


class RunnerCredentialOut(Schema):
    """The runner's own fetch — actual token values (HTTPS + PAT-authed, owner-gated)."""

    claude_token: str = ""
    github_token: str = ""
    op_sa_token: str = ""
    updated_at: dt.datetime | None = None


class RunnerCredentialStatusOut(Schema):
    """Masked view — booleans, never values. The POST response + any UI."""

    has_claude_token: bool = False
    has_github_token: bool = False
    has_op_sa_token: bool = False
    updated_at: dt.datetime | None = None


# ---------------------------------------------------------------------------
# Readiness drills (spec 2026-07-24-directed-runner-routing, Task 7)
# ---------------------------------------------------------------------------


class RunnerDrillOut(Schema):
    id: int
    agent_slug: str
    outcome: str
    summary: str
    started_at: dt.datetime
    finished_at: dt.datetime | None
    turn_id: uuid.UUID | None

    @staticmethod
    def resolve_agent_slug(obj):
        return obj.agent.slug


class DrillIn(Schema):
    agents: list[str] | None = None


class DrillReportIn(Schema):
    outcome: Literal["pass", "fail"]
    summary: str = ""
