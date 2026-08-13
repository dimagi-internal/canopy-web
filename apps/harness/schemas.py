"""Pydantic schemas for the /api/harness surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from canopy_cron import validate_cron, validate_timezone
from ninja import Schema
from pydantic import Field, field_validator

# Kept in lockstep with Turn.ORIGIN_CHOICES / Turn.ROUTING_CHOICES (models.py).
# These are the values the DB columns accept (origin max_length=32, routing
# max_length=15); typing the INPUT schemas as Literals turns an out-of-set value
# into a 422 at the API boundary instead of a Postgres "value too long" 500 that
# SQLite CI can't reproduce. Output schemas stay `str` — they serialize values the
# DB already validated, and a Literal there would break on any legacy row.
#
# POST-able sources only. `canopy_web_chat` / `canopy_scheduler` are server-set
# (one in-repo producer each) — a caller spelling them would borrow that source's
# routing rule. Retired spellings are normalized, not rejected: the live fleet
# posts `cron`/`manual` today and 422'ing them would break Echo/Ada mid-flight.
Origin = Literal[
    "api", "ace_web", "email", "slack",           # postable
    "board", "cron", "manual", "drill",           # legacy, normalized below
]
# What a per-agent routing rule may name — the full vocabulary, including the
# server-only sources (a rule NAMES a source, it does not produce one).
RoutableSource = Literal[
    "ace_web", "email", "canopy_scheduler", "canopy_web_chat", "slack", "api",
]
Routing = Literal["prefer_local", "local_only", "any"]


def normalize_origin(value: str) -> str:
    """Map a retired spelling onto its replacement. Shared by every input schema
    carrying an `origin`, so the boundary can't normalize inconsistently.

    `enqueue_turn` applies the SAME mapping, deliberately: TurnSpec.from_dict
    parses stored Item JSON with no schema in the path, so a request-boundary-only
    rule would miss every Item raised before this shipped.
    """
    from apps.harness.models import Turn

    return Turn.LEGACY_ORIGIN_ALIASES.get(value, value)


class RunnerIn(Schema):
    name: str
    kind: str  # emdash|cloud|remote
    capabilities: dict = {}
    host: str = ""  # macOS user@hostname — load-bearing for session reuse across accounts
    workspace: str = ""  # tenant slug; defaults to the pairer's default workspace


class UnclaimableTurnOut(Schema):
    """A queued turn no online runner can claim — surfaced so a stall is loud."""
    turn_id: str
    target: str
    prompt: str
    created_at: dt.datetime
    reason: str
    # "config" = nothing declares this target (needs a fix); "offline" = something
    # does, but no runner is reachable right now (usually transient).
    kind: str = "config"


class RunnerCapabilitiesIn(Schema):
    # Wholesale replacement, like the skill catalog — the caller sends the full
    # capabilities it wants (e.g. {"agents": [...], "projects": ["canopy-web"]}).
    capabilities: dict


class DrillRollup(Schema):
    """Aggregated readiness-drill outcomes for one runner, across all its
    (runner, agent) drill pairs — the supervisor's at-a-glance signal, without
    a client-side fetch-and-reduce over /runners/{id}/drills."""

    passed: int
    failed: int
    pending: int
    last_finished_at: dt.datetime | None


class RunnerOut(Schema):
    id: uuid.UUID
    name: str
    kind: str
    status: str
    status_note: str
    ready: bool
    ready_note: str
    # Served alongside `status` (which already reads "paused" via live_status)
    # because the two answer different questions: `status` says the box is not
    # taking work, these say a HUMAN decided that and why. A client that only
    # renders status still behaves correctly — that is the point of deriving it
    # in live_status — but it cannot explain itself without these.
    paused: bool
    paused_note: str
    paused_at: dt.datetime | None
    last_heartbeat_at: dt.datetime | None
    capabilities: dict
    host: str
    code_branch: str
    code_version: str
    code_sha: str
    # The sha the SERVER expects (settings.RUNNER_CODE_SHA) — the same quantity
    # `code_sha` holds, computed at image-build time. Denormalized onto every row
    # rather than served from a second endpoint: the client needs it per row to
    # decide anything, and one string repeated N times beats a second fetch for
    # a page that already has this one. `can_manage` sets the precedent for a
    # derived, non-column field on this schema.
    expected_code_sha: str
    # The two halves of the ORDERING, mirroring the sha pair above: without them
    # `code_sha != expected_code_sha` can only say "different", and the supervisor
    # was rendering that as "behind" (see Runner.code_committed_at).
    code_committed_at: int
    expected_code_committed_at: int

    workspace: str | None
    # The human who paired the runner. This — NOT `workspace` — is what governs
    # what the runner may WORK FOR (claim_next_turn derives the tenant from the
    # pairer's workspace memberships, so a runner serves agents across every
    # workspace its pairer belongs to). `workspace` is only the home/visibility
    # tenant. Surfaced so the supervisor can show the meaningful owner instead of
    # implying a single-workspace serving scope.
    paired_by_email: str | None
    # Whether THIS caller may mutate the runner (declare capabilities, retire,
    # heartbeat/claim as it) — a property of the (caller, runner) pair, so
    # list_runners stamps it on each row; a Ninja resolver only sees the row.
    #
    # Defaults True because every OTHER route returning a RunnerOut resolves its
    # runner through `_runner_or_404`, which IS the act-on gate — reaching one of
    # those responses at all proves the caller can manage it. Only the list can
    # legitimately contain a runner the caller may not act on, and only the list
    # sets this per row.
    can_manage: bool = True
    # None when this runner has never been drilled (not "zero of zero pass") —
    # resolved from RunnerDrill rows via `.drills`, see resolve_drill_rollup.
    drill_rollup: DrillRollup | None = None

    @staticmethod
    def resolve_expected_code_sha(obj) -> str:
        # Per KIND, because the fleet runs two different programs: a laptop
        # executes runner/canopy_runner, a cloud box executes runner/ec2. Serving
        # one sha to both would mark every cloud runner permanently stale — the
        # alert would then be pure noise on exactly the boxes it was extended to
        # cover. Either being empty still means UNKNOWN and stays silent.
        # The derivation lives on the model so the heartbeat's update nudge
        # compares the same quantity this serves.
        return obj.expected_code_sha()

    @staticmethod
    def resolve_expected_code_committed_at(obj) -> int:
        from django.conf import settings
        from .models import Runner

        setting = (
            "RUNNER_CLOUD_CODE_COMMITTED_AT" if obj.kind == Runner.CLOUD
            else "RUNNER_CODE_COMMITTED_AT"
        )
        try:
            return int(getattr(settings, setting, 0) or 0)
        except (TypeError, ValueError):
            # A malformed build arg must not 500 the whole runners list — unknown
            # (0) simply means the alert keeps today's direction-less behaviour.
            return 0

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

    @staticmethod
    def resolve_drill_rollup(obj) -> DrillRollup | None:
        rows = list(obj.drills.all())
        if not rows:
            return None
        return DrillRollup(
            passed=sum(1 for d in rows if d.outcome == "pass"),
            failed=sum(1 for d in rows if d.outcome == "fail"),
            pending=sum(1 for d in rows if d.outcome == "pending"),
            last_finished_at=max((d.finished_at for d in rows if d.finished_at), default=None),
        )


class PauseIn(Schema):
    # Why this box is parked, for whoever finds it idle later. A pause with no
    # reason is indistinguishable from a broken runner at a glance, and the
    # cost of THIS feature going wrong is a box that stays silent long after
    # the reason expired.
    note: str = ""


class HeartbeatIn(Schema):
    active_turn_ids: list[str] = []
    degraded: bool = False
    note: str = ""
    host: str = ""  # refresh the owning macOS host (in case a runner row is reused)
    ready: bool = True   # can the runner fire a turn (cdp healthy ∧ not recently failed)
    ready_note: str = ""
    code_branch: str = ""  # the runner checkout's git branch — supervisor alerts on non-main
    code_version: str = ""  # the runner package's __version__ (legible; not the comparison)
    # The sha of the last commit touching the runner's own source. Compared against
    # settings.RUNNER_CODE_SHA; empty means unknown and never alerts.
    code_sha: str = ""
    # Committer epoch of that same commit. 0 = unknown; see Runner.code_committed_at
    # for why an ORDER is needed on top of the identity a sha gives.
    code_committed_at: int = 0
    # The repos this runner can actually drive, OBSERVED (emdash's own projects
    # table on a laptop; the configured list on a cloud box) rather than typed by
    # a human at pairing — which drifted silently and only ever toward "cannot
    # run". Replaces `capabilities["projects"]` wholesale when present.
    #
    # None (absent) and [] mean DIFFERENT things and the difference is the whole
    # safety property: absent = "I could not tell this tick" (an unreadable emdash
    # DB) and leaves the stored list alone; [] = "I genuinely have none" (a fresh
    # box) and empties it. Treating absence as empty would blank the list and make
    # every repo turn on this runner unclaimable — the `replace_reported_sessions`
    # drift, one notch worse. It also makes rollout free: a runner on old code
    # sends nothing and keeps its list.
    projects: list[str] | None = None


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
    # Emdash's own per-conversation liveness flag: "working" | "awaiting-input" | "".
    # Defaulted because "" is a real and common answer — a runner that predates this,
    # a cloud runner with no emdash, or an emdash whose schema drifted all send
    # nothing, and the server falls back to its activity-recency heuristic for them
    # (services.is_session_running). Only a runner that actually knows overrides it.
    agent_status: str = ""
    last_interacted_at: dt.datetime | None = None
    recent_messages: list = []  # Phase B populates this; ignored/empty in Phase A
    # The dialog this session is blocked on, read from its transcript, or None
    # for "I looked and there is none". `None` is a REAL answer and is written
    # through: it is what retires a menu once the human answers at the laptop.
    #
    # Defaulted so a runner that predates this keeps working — but note what its
    # default MEANS. An old runner sends nothing, which lands as None and clears
    # the field, i.e. "no dialog". That is the safe direction: the failure is a
    # phone with no buttons (the terminal still answers), never a phone offering
    # buttons against a dialog that is gone.
    question: dict | None = None


class ReportSessionsIn(Schema):
    sessions: list[ReportedSessionIn] = []
    # emdash task names this runner has seen ARCHIVED. Defaulted so an older runner
    # (which does not send it) keeps working unchanged — it simply never closes a row.
    archived: list[str] = []


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
    # Name the box explicitly. A pin bypasses assignments and source rules — never
    # the tenant gate, never one_executing_turn_per_agent. This is what retired the
    # `drill` origin: a drill is an api turn that names its runner, identified by
    # its RunnerDrill row rather than by a magic origin value.
    runner_id: uuid.UUID | None = None

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


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
    status: str  # done|failed|cancelled — a runner's own cancel_turn interrupt
    # finishes the turn cancelled (see the CDP-interrupt cancel flow); done and
    # failed remain the normal completion outcomes.
    result_note: str = ""
    # The emdash session this turn drove, when it drove one. Written to the turn so
    # the agent's later close-out can be matched to it; a failed turn that never got
    # a session simply omits it.
    emdash_task_id: str = ""


class TranscriptAppendIn(Schema):
    """Raw `claude -p` JSONL lines to append — one element per JSONL record,
    verbatim (see services.append_transcript). Never re-encoded or parsed."""

    lines: list[str]
    # Optional per-batch idempotency key (security review 2026-07-26, F5): if
    # this matches the LAST batch actually applied to the turn, the append is
    # a no-op (a retry after a lost response), not a double-append. Omit to
    # skip dedup entirely — older/simpler callers are unaffected.
    batch_id: str = ""


class TranscriptAppendOut(Schema):
    line_count: int
    bytes_raw: int
    # True once this turn's transcript has hit services.TRANSCRIPT_TURN_MAX_BYTES
    # (F2) — every batch from here on is silently dropped, so a runner can stop
    # bothering to flush further content for this turn.
    truncated: bool


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
    # The server-side catch-up marker: max persisted turn_index for the session
    # (None = no rows yet). The runner ships transcript records AFTER this on
    # attach, so a restart/failover never loses the resume point.
    last_index: int | None = None
    # The OLDEST turn_index the server holds (None = no rows). Paired with
    # last_index so the runner can distinguish "you are up to date, send what's
    # new" from "you are missing the head, send everything": a max alone only ever
    # licenses appending above the high-water mark, so a session whose beginning
    # was never captured could never repair itself no matter how long it streamed.
    first_index: int | None = None
    # Whether a viewer is attached. Governs live fan-out ONLY — rows are persisted
    # for every session either way (see list_streams). Old runners ignore it, which
    # is safe: they simply keep streaming exactly the sessions they used to.
    live: bool = True


class StreamSyncOut(Schema):
    streams: list[StreamDescriptorOut] = []


class LiveEventIn(Schema):
    kind: str
    seq: int
    # Transcript record ordinal (raw index into the session's .jsonl). -1 = an
    # old runner that doesn't send ordinals; such events stay live-view-only
    # (persisting assistant-only rows would kill the tail fallback's user side).
    index: int = -1
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
    # Transcript record ordinal. -1 = an old runner; the server then keeps the
    # legacy write-once contract (sequential, only into an empty session).
    index: int = -1
    # Structured fields for a non-prose row — a tool_use's {id,name,input}, a
    # tool_result's {tool_use_id,is_error}. Empty for plain text. Stored as the
    # Message's content so history renders identically to the live stream.
    content: dict = {}


class SessionBackfillIn(Schema):
    session_id: uuid.UUID
    messages: list[BackfillMessageIn] = []
    # False = "more chunks follow"; the server keeps `backfill_requested` set so a
    # ship that dies halfway is retried whole rather than leaving a partial history
    # behind a cleared flag. A transcript has to be chunked at all because the whole
    # payload used to go in ONE request against DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB),
    # which Django raises as an unhandled 500 BEFORE the view runs — measured over
    # 193 local transcripts, one already exceeds it and three more are past 1.9 MB.
    # Defaults True so an old runner, which posts exactly once, is unaffected.
    final: bool = True


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

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


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

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


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
    claude_token_secondary: str | None = None
    claude_api_key: str | None = None
    github_token: str | None = None
    op_sa_token: str | None = None


class RunnerCredentialOut(Schema):
    """The runner's own fetch — actual token values (HTTPS + PAT-authed, owner-gated)."""

    claude_token: str = ""
    claude_token_secondary: str = ""
    claude_api_key: str = ""
    github_token: str = ""
    op_sa_token: str = ""
    updated_at: dt.datetime | None = None


class RunnerCredentialStatusOut(Schema):
    """Masked view — booleans, never values. The POST response + any UI."""

    has_claude_token: bool = False
    has_claude_token_secondary: bool = False
    has_claude_api_key: bool = False
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


class MenuAnswerOut(Schema):
    session_id: str
    session_key: str
    answer_id: str
    option: int | None = None
    #: One list of chosen option numbers per declared question. A runner that
    #: predates this ignores the field and presses `option`, which is what it
    #: does today — so the poll tick never gets WORSE than the current
    #: behaviour on an ask this shape cannot express.
    selections: list[list[int]] | None = None
    texts: list[str | None] | None = None


class MenuAnswerSyncOut(Schema):
    answers: list[MenuAnswerOut] = []


class MenuAnswerResultOut(Schema):
    ok: bool


class MenuAnswerResultIn(Schema):
    session_id: uuid.UUID
    answer_id: str
    # What the runner did with it. Free-form on purpose: the vocabulary lives in
    # `canopy_runner.hooks` (answered / no_dialog / wrong_pane / …) and the server
    # only needs to know the answer is retired, not to re-litigate the outcome.
    outcome: str = ""


class CloseOut(Schema):
    session_id: str
    session_key: str


class CloseSyncOut(Schema):
    closes: list[CloseOut] = []
