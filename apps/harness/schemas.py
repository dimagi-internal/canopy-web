"""Pydantic schemas for the /api/harness surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from canopy_cron import validate_cron, validate_timezone
from canopy_sdk import contract
from ninja import Schema
from pydantic import Field, field_validator, model_validator

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
    kind: str  # emdash|cloud|remote|desktop
    capabilities: dict = {}
    host: str = ""  # macOS user@hostname — load-bearing for session reuse across accounts
    workspace: str = ""  # tenant slug; defaults to the owner's default workspace


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


class HealthCheck(Schema):
    """One feature a runner checked on itself. `warn` is "works, but a person
    should know" (one Claude credential, no fallback); `fail` is "this feature
    is off" (a package that did not import, so no transcripts)."""

    name: str = Field(max_length=64)
    status: Literal["ok", "warn", "fail"]
    detail: str = Field(default="", max_length=2000)


class RunnerHealthIn(Schema):
    checks: list[HealthCheck] = Field(default_factory=list, max_length=64)
    # Runner-clock epochs. `bootstrapped_at` discharges a refresh request
    # (Runner.refresh_pending), so it must be the time the LAST bootstrap
    # finished — not when this check list was built.
    checked_at: float = 0
    bootstrapped_at: float = 0


class DrillRollup(Schema):
    """Aggregated readiness-drill outcomes for one runner, across all its
    (runner, agent) drill pairs — the supervisor's at-a-glance signal, without
    a client-side fetch-and-reduce over /runners/{id}/drills."""

    passed: int
    failed: int
    pending: int
    last_finished_at: dt.datetime | None


class RunnerFlagsIn(Schema):
    flags: list[str]


class RunnerEngineIn(Schema):
    # emdash | claude-desktop (Runner.ENGINE_CHOICES). Validated in the view so the
    # error names the choices rather than a pydantic literal mismatch.
    engine: str


class RunnerOut(Schema):
    id: uuid.UUID
    name: str
    kind: str
    # The session runtime this (laptop) runner opens NEW sessions in — emdash or
    # claude-desktop. The runner reads it off its own heartbeat response, so this
    # field is also how a flip reaches the box (canopy-web#1188).
    engine: str = "emdash"
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
    # The runner's OWNER — the human whose token it authenticates with. This — NOT `workspace` — is what governs
    # what the runner may WORK FOR (claim_next_turn derives the tenant from the
    # owner's workspace memberships, so a runner serves agents across every
    # workspace its owner belongs to). `workspace` is only the home/visibility
    # tenant. Surfaced so the supervisor can show the meaningful owner instead of
    # implying a single-workspace serving scope.
    owner_email: str | None
    # DEPRECATED compat alias of `owner_email` (the field was `paired_by_email`
    # until 2026-10-02). Runners and the canopy CLI update independently of this
    # server, so ones installed before the rename still read this key. Remove once
    # every runner reports a code_sha at/after the rename and canopy >= the release
    # that reads `owner_email` is the floor (target: 2026-10-16).
    paired_by_email: str | None = None
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
    # Whether the caller may ADMINISTER this box (credentials, browser sign-in)
    # as distinct from speaking AS it (drills, pause, claim). Separate flags
    # because they gate different routes: reporting one for the other is how a
    # UI ends up rendering a control that 404s.
    can_administer: bool = True
    # None when this runner has never been drilled (not "zero of zero pass") —
    # resolved from RunnerDrill rows via `.drills`, see resolve_drill_rollup.
    drill_rollup: DrillRollup | None = None
    # What the box reports about its own features (Runner.health), keyed by
    # check name. A map rather than a list on purpose: openapi-fetch's Readable<T>
    # degrades an array of OBJECTS inside a response into an ArrayLike the client
    # cannot assign back to the generated type (see frontend/src/api/agents.ts
    # toPage); a record of objects survives it. None = the box does not report —
    # the laptops, an older cloud box — which is unknown, never "healthy".
    health_checks: dict[str, HealthCheck] | None = None
    # Server clock, stamped on receipt: a box that stops reporting keeps its last
    # list, and this is how a reader tells a current answer from an old one.
    health_received_at: dt.datetime | None = None
    health_bootstrapped_at: float | None = None
    # Which mailboxes the box reports it can read (Runner.mailboxes_readable).
    # None = never reported (unknown), [] = it can read none.
    mailboxes_readable: list[str] | None = None
    mailboxes_checked_at: dt.datetime | None = None

    # What this box's owner has declared about it (RunnerFlag) — e.g. `zdr`.
    flags: list[str] = []

    @staticmethod
    def resolve_flags(obj) -> list[str]:
        return sorted(obj.flags)

    # Every flag an owner may declare — the contract's list, served per row (as
    # `expected_code_sha` is) so the UI draws one checkbox per known flag and
    # keeps no list of its own.
    known_flags: list[str] = []

    @staticmethod
    def resolve_known_flags(obj) -> list[str]:
        return sorted(contract.RUNNER_FLAGS)
    refresh_requested_at: dt.datetime | None = None
    # Asked to refresh and has not bootstrapped since. The runner reads this off
    # its own heartbeat reply — the durable request, not a frame.
    refresh_pending: bool | None = None
    # canopy's canonical address. A runner still configured with an address
    # canopy has left reads it off its heartbeat reply and moves itself there.
    canonical_base_url: str | None = None

    @staticmethod
    def resolve_health_checks(obj) -> dict[str, HealthCheck] | None:
        if not obj.health:
            return None
        checks = {c["name"]: HealthCheck(**c) for c in obj.health.get("checks") or []}
        # `github.<slug>` is canopy-web's own delegation state, which the box only
        # asks about at boot. Served from the box's report it stayed red for a
        # token lent after that boot, under a fresh `health_received_at` (eva,
        # 2026-10-03). So it is answered here, from the rows, on every read.
        from apps.agents import delegations

        from .services import agents_served_by

        checks = {n: c for n, c in checks.items() if n != "github" and not n.startswith("github.")}
        for agent in agents_served_by(obj):
            status, detail = delegations.readiness(agent)
            checks[f"github.{agent.slug}"] = HealthCheck(
                name=f"github.{agent.slug}", status=status, detail=detail)
        return checks

    @staticmethod
    def resolve_health_received_at(obj) -> dt.datetime | None:
        raw = (obj.health or {}).get("received_at")
        return dt.datetime.fromisoformat(raw) if raw else None

    @staticmethod
    def resolve_health_bootstrapped_at(obj) -> float | None:
        try:
            return float((obj.health or {}).get("bootstrapped_at") or 0) or None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def resolve_refresh_pending(obj) -> bool:
        return obj.refresh_pending()

    @staticmethod
    def resolve_canonical_base_url(obj) -> str:
        from django.conf import settings

        return (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")

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
    def resolve_owner_email(obj) -> str | None:
        return obj.owner.email if obj.owner_id else None

    @staticmethod
    def resolve_paired_by_email(obj) -> str | None:
        return obj.owner.email if obj.owner_id else None

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
    # The profile-enforcement version this runner can honour — whether it may be
    # given a CALLER's turn, confined to a capability (services.profile_q).
    # Absent (an older runner) means 0: never.
    profiles: int = 0
    # The caller-envelope version the runner's code reads: 2 = it understands
    # `profile: "confined"`. Absent (older code) means 0, and such a box gets no
    # caller turns (services.profile_q, ENVELOPE_VERSION).
    envelope: int = 0
    # 1 = this runner's code delivers a follow-up INTO a conversation's running
    # turn (`Turn.rides_turn`, canopy-web#1153). Absent (older code) means 0:
    # it is never handed one, because it would bridge the same reply twice.
    midturn: int = 0
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
    # The box's own feature checks (see Runner.health). None = not reported this
    # beat, and leaves the stored value alone — the same absent-is-not-empty rule
    # as `projects`, so a beat from a path that does not build the list (a lease
    # renewer) cannot wipe it.
    health: RunnerHealthIn | None = None
    # The mailboxes this box PROVED it can read (a token that answered a search),
    # lowercased. Same absent-is-not-empty rule as `projects`: None = not
    # reported this beat (an older runner, or a beat from a path that does not
    # probe) and leaves the stored list alone; [] = "I can read none of them".
    # The doorbell rings only boxes whose list holds the address (or that never
    # reported one) — see apps/inbound/services.online_runners_for.
    mailboxes_readable: list[str] | None = None


class ResolveSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the owner)
    thread_key: str


#: The retired spelling of `session_key`, still accepted on input.
#:
#: The field was renamed when the cloud runner started putting Claude session ids
#: in it (harness migration 0057). Runners update on deploy, but an agent's
#: `canopy agent turn` client and a runner mid-update still send the old name —
#: so each input takes either, and the new name wins when both arrive. Remove the
#: field and `adopt_legacy_session_key` once nothing in the fleet sends it.
LEGACY_SESSION_KEY = Field(
    default="", max_length=200, json_schema_extra={"deprecated": True},
    description="Deprecated: send `session_key`.",
)


def adopt_legacy_session_key(model):
    """An after-validator body: fold `emdash_task_id` into an empty `session_key`.
    After, not before, so it works the same on ninja `Schema` (which wraps the raw
    input before a before-validator sees it) and on plain pydantic models."""
    if model.emdash_task_id and not model.session_key:
        model.session_key = model.emdash_task_id
    return model


class ResolveSessionOut(Schema):
    reuse: bool
    new_thread: bool
    session_key: str
    # Deprecated duplicate of `session_key`, for runners that predate the rename.
    emdash_task_id: str
    agent_task_ext_id: str
    summary: str
    link_id: str | None


class RecordSessionIn(Schema):
    agent_slug: str = ""
    project: str = ""  # set instead of agent_slug for a repo session
    workspace: str = ""  # required with project: the turn's tenant (gates the owner)
    thread_key: str
    session_key: str = ""
    session_id: str = ""
    agent_task_ext_id: str | None = None
    summary: str | None = None
    # A readable name for the session, used where the session would otherwise be
    # named after its key. A laptop's key is an emdash task name a person can
    # read; a cloud runner's is a Claude session UUID, so it sends one of these.
    title: str = Field(default="", max_length=200)
    # The turn this session is running, when the runner is recording it mid-turn.
    # Stamps that turn's session key NOW rather than at finish, so the agent's
    # close-out — which it posts before the turn ends — can find its turn.
    turn_id: uuid.UUID | None = None
    emdash_task_id: str = LEGACY_SESSION_KEY

    _legacy_key = model_validator(mode="after")(adopt_legacy_session_key)


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
    # The runner's dissent from its OWN `agent_status` above: emdash says this session
    # is not working, but it is still writing to its transcript (or to a subagent's).
    # Set only after the runner has watched writes land AFTER the flag went non-working
    # — see canopy_runner.sessions.annotate_engine_staleness for why that, and not
    # recency, is the discriminator. Defaulted False so an older runner, which cannot
    # dissent, keeps the pre-existing "trust the flag outright" behaviour exactly.
    agent_status_stale: bool = False
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
    # True when `sessions` is this runner's WHOLE open set (it was not cut off by
    # the runner's report limit). Only then is absence an observation: a session
    # missing from consecutive complete reports was closed. Defaulted False so an
    # older runner never has anything retired on absence.
    complete: bool = False


class EmdashSessionOut(Schema):
    id: uuid.UUID
    emdash_task: str
    #: `SessionView.project` carries `emdash_project`, so this stays meaningful
    #: for an agent-owned row whose stored column the XOR necessarily cleared.
    project: str
    #: Whose work this is. Absent until now, which every consumer doing
    #: `.get("agent")` read as `agent: null` — indistinguishable from "known to
    #: belong to nobody" — leaving `project` as the only thing to guess from.
    agent: str | None
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


class ParentIn(Schema):
    """What the caller was running INSIDE when it asked for this work — recorded
    on the new turn/session as `parent_*` + `provenance.parent`. Every field is
    optional and none is checked: an id that does not resolve is recorded as
    given and never refuses the request. The `X-Canopy-Parent-Turn`,
    `X-Canopy-Parent-Session` and `X-Canopy-Claude-Session` headers say the same
    thing; a field here wins over its header. Nothing runner-specific: the parent
    turn names the runner that claimed it."""

    turn: str = ""
    session: str = ""
    claude_session: str = ""


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
    # Ask for the MODE this agent turn runs in, above every routing rule and the
    # agent's own switch (apps/harness/turn_mode.py). `manual` is open to anyone
    # who may enqueue — lowering autonomy is always safe. `auto` only from the
    # agent's owner or an admin (workspace owners included), signed in or on a
    # PAT; anyone else gets a 403. Refused on a project turn and on email, which a
    # runner posts on a stranger's behalf. Omit to let the rules decide.
    turn_mode: Literal["auto", "manual"] | None = None
    # What this request was made from (see ParentIn). Optional.
    parent: ParentIn | None = None

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


class InitiatorPersonOut(Schema):
    id: int
    email: str
    name: str


class InitiatorOut(Schema):
    """Who asked for this turn, and how that was established."""

    kind: str
    via: str
    assurance: str
    user: InitiatorPersonOut | None = None
    contact: InitiatorPersonOut | None = None
    agent: str | None = None
    # WHICH credential the request that created the turn used — {type, id, label}
    # (`pat`, `oauth`, `session`, `delegated`, `contact`, `caller_token`) — and
    # the program that sent it (X-Canopy-Client). Null when canopy started it.
    credential: dict | None = None
    client: str = ""
    # The turn / session it was started from, and the runner that parent turn ran
    # on ({id, name, kind}) — see TurnOut.parent_*.
    parent: dict | None = None


class TurnOut(Schema):
    id: uuid.UUID
    # Exactly one of these is set — a turn targets an agent or a repo, never
    # both. Consumers should read `target` unless they specifically need to know
    # which kind it is.
    agent_slug: str | None
    project: str
    target: str
    # The tenant the runner must pass back to record/resolve a PROJECT session
    # link (the owner may belong to several workspaces; the turn knows its own).
    # Derived: agent turns report their agent's workspace, project turns their own.
    workspace_slug: str | None
    origin: str
    status: str
    routing: str
    prompt: str
    origin_ref: dict
    claimed_by_name: str | None
    enqueued_by_email: str | None
    initiator: InitiatorOut
    # manual | auto, decided at claim (apps/harness/turn_mode.py); "" until then.
    turn_mode: str = ""
    turn_mode_basis: str = ""
    # What the dispatcher ASKED for (TurnIn.turn_mode) and who, "" / null if
    # nothing. `turn_mode` above is what the claim actually decided.
    requested_turn_mode: str = ""
    requested_turn_mode_by_email: str | None = None
    # The runner this turn is PINNED to (TurnIn.runner_id), if any — only it may claim.
    pinned_runner_id: uuid.UUID | None = None
    pinned_runner_name: str | None = None
    # Set when this turn was claimed to be DELIVERED INTO its conversation's
    # running turn (that turn's id) rather than run on its own: the runner types
    # it into the live session and it finishes when that turn does.
    rides_turn_id: uuid.UUID | None = None
    # WHAT created this turn (apps/harness/provenance.py): credential, client,
    # user_agent, request_id, mcp_tool, parent (raw), clicked_by. The client ip
    # is recorded but not served.
    provenance: dict = {}
    parent_turn_id: uuid.UUID | None = None
    parent_session_id: uuid.UUID | None = None
    parent_task: str = ""
    parent_claude_session: str = ""
    # The board task whose approved dispatch created this turn, if any — by its
    # ext_id, the way every route names a task (it is the agent's own task).
    raised_from_task_ext_id: str | None = None
    session_id: str
    result_note: str
    # True when `prompt`, `origin_ref` and `result_note` were blanked because
    # the caller may not read this turn's content (apps/harness/turn_access.py).
    content_hidden: bool = False
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
    def resolve_raised_from_task_ext_id(obj) -> str | None:
        if not getattr(obj, "raised_from_task_id", None):
            return None
        return obj.raised_from_task.ext_id

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

    @staticmethod
    def resolve_requested_turn_mode_by_email(obj) -> str | None:
        return obj.requested_turn_mode_by.email if obj.requested_turn_mode_by_id else None

    @staticmethod
    def resolve_pinned_runner_name(obj) -> str | None:
        return obj.pinned_runner.name if obj.pinned_runner_id else None

    @staticmethod
    def resolve_provenance(obj) -> dict:
        from .provenance import public

        return public(getattr(obj, "provenance", None))

    @staticmethod
    def resolve_initiator(obj) -> dict:
        # Distinct from enqueued_by_email: that is the CALLER of the enqueue (a
        # runner, for an email turn); this is the person the turn is for.
        from .initiator import describe

        return describe(obj)


class CallerContextOut(Schema):
    """Who asked for a turn and what canopy knows about them — the caller
    envelope (`apps/harness/caller_context.py`). Loosely typed on purpose: it is
    a versioned document the runner writes to disk for the agent, not a surface
    a client binds to field by field."""

    envelope: dict


class ClaimedTurnOut(TurnOut):
    """A turn as its CLAIMING runner receives it: everything `TurnOut` has, plus
    the caller envelope, so the runner can hand it to the agent without a second
    round trip. Only the claim returns it — it carries what canopy knows about a
    person, which a turn LISTING has no reason to spread."""

    caller_context: dict
    # For a CONFINED turn only: the token the session uses for canopy's MCP in
    # place of the runner owner's PAT (harness.models.CallerToken). Null otherwise.
    mcp_token: str | None = None
    # For a CHAT turn: the chat's key (canopy_sessions.ChatKey), which the runner
    # gives only to the Claude session driving this chat. Presented in the
    # `X-Canopy-Chat-Key` header, it reaches this chat's secrets and page and
    # nothing else. Null for a turn that is not a chat's.
    chat_key: str | None = None

    # Nothing about a visitor's HOST credential is ever here (host grant
    # contract v1): the gateway (`site_call`) attaches it inside canopy-web, so
    # it cannot leak through a runner. The canopy-minted `on_behalf_of`
    # assertion that used to ride this response was deleted on 2026-09-26
    # before anything consumed it.

    @staticmethod
    def resolve_mcp_token(obj) -> str | None:
        return getattr(obj, "mcp_token", None)

    @staticmethod
    def resolve_chat_key(obj) -> str | None:
        return getattr(obj, "chat_key", None)

    @staticmethod
    def resolve_caller_context(obj) -> dict:
        from .caller_context import build

        return build(obj)


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
    # The session this turn drove, when it drove one (an emdash task on a laptop, a
    # Claude session id on a cloud runner). Written to the turn so the agent's
    # close-out can be matched to it; a failed turn that never got a session simply
    # omits it — which is also what marks it safe to re-run.
    session_key: str = ""
    emdash_task_id: str = LEGACY_SESSION_KEY

    _legacy_key = model_validator(mode="after")(adopt_legacy_session_key)


class TranscriptAppendIn(Schema):
    """Raw `claude -p` JSONL lines to append — one element per JSONL record,
    verbatim (see services.append_transcript). Never re-encoded or parsed."""

    lines: list[str]
    # Optional per-batch idempotency key (security review 2026-07-26, F5): if
    # this matches the LAST batch actually applied to the turn, the append is
    # a no-op (a retry after a lost response), not a double-append. Omit to
    # skip dedup entirely — older/simpler callers are unaffected.
    batch_id: str = ""


class TurnMessageOut(Schema):
    turn_index: int
    role: str
    content: dict
    plaintext: str


class TurnMessagesOut(Schema):
    messages: list[TurnMessageOut]
    #: The view stopped at services.TRANSCRIPT_VIEW_MAX_MESSAGES; the raw
    #: transcript route has the rest.
    truncated: bool


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
    # WHEN — exactly one. `cron` is a recurring schedule; `run_once_at` is a
    # one-off that fires at that instant and then disables itself (its cron is
    # derived server-side).
    cron: str | None = None
    run_once_at: dt.datetime | None = None
    timezone: str = "UTC"
    enabled: bool = True
    routing: str = "prefer_local"
    grace_minutes: int = 120
    always_run: bool = False
    notify: list[str] = ["inbox"]

    @field_validator("cron")
    @classmethod
    def _check_cron(cls, v: str | None) -> str | None:
        return validate_cron(v) if v is not None else v

    @model_validator(mode="after")
    def _exactly_one_when(self):
        if bool(self.cron) == bool(self.run_once_at):
            raise ValueError("give exactly one of cron (recurring) or run_once_at (one-off)")
        return self

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
    # Setting `cron` makes the schedule recurring; setting `run_once_at` makes it
    # a one-off (and re-arms one that already fired). Not both.
    cron: str | None = None
    run_once_at: dt.datetime | None = None
    timezone: str | None = None
    enabled: bool | None = None
    routing: str | None = None
    grace_minutes: int | None = None
    always_run: bool | None = None
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
    # Set for a one-off: it fires once, at this instant, then `enabled` goes false.
    # `cron` is then derived from it and is shown only for runner compatibility.
    run_once_at: dt.datetime | None = None
    timezone: str
    enabled: bool
    routing: str
    grace_minutes: int
    always_run: bool = False
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
    # WHICH transcript the first_index/last_index markers above were computed from
    # (the Claude session uuid). The markers are per-file ordinals, so they mean
    # nothing against a different file — a runner that resolves a transcript whose
    # id disagrees with this must ignore them and ship the whole history. "" = the
    # server has no provenance for its rows, which is treated the same way.
    transcript_id: str = ""


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
    # The transcript these ordinals index into (the Claude session uuid). When it
    # differs from what the binding recorded, the session's derived rows belong to
    # a DIFFERENT conversation and are dropped before this batch is written — see
    # canopy_sessions.services.ensure_transcript_identity. "" = an old runner,
    # which makes no provenance claim and is left alone.
    transcript_id: str = ""


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
    # The transcript this history came from — same contract as SessionStreamIn's.
    # Only the FIRST chunk can drop anything: once it records the id, the rest of
    # the ship matches and writes straight through, so a chunked backfill can
    # never delete the chunks that preceded it.
    transcript_id: str = ""


class BackfillWriteOut(Schema):
    written: int


# ---------------------------------------------------------------------------
# Deferred turns — what a task runs when it is approved
# ---------------------------------------------------------------------------


class TurnSpecIn(Schema):
    """One deferred Turn enqueue. `target_agent=""` means the task's own agent —
    self-dispatch is the default; Ada's fan-out is this field set."""

    prompt: str = ""
    target_agent: str = ""
    origin: Origin = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    routing: Routing = "prefer_local"

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


# ---- Runner credentials (per-runner, cloud-only; laptop uses emdash) ----
class RunnerCredentialIn(Schema):
    """Set a cloud runner's credentials. A field left None is unchanged
    (non-clobbering) — update just the Claude token without wiping the rest."""

    claude_token: str | None = None
    claude_token_secondary: str | None = None
    claude_api_key: str | None = None
    #: Whose subscription each login is (an email, or any name). Not a secret.
    claude_token_label: str | None = Field(default=None, max_length=200)
    claude_token_secondary_label: str | None = Field(default=None, max_length=200)


class RunnerCredentialOut(Schema):
    """The runner's own fetch — actual token values (HTTPS + PAT-authed, owner-gated)."""

    claude_token: str = ""
    claude_token_secondary: str = ""
    claude_api_key: str = ""
    updated_at: dt.datetime | None = None


class DroppedRouteOut(Schema):
    agent: str
    # "" = the agent's default runner order; otherwise the source a rule is for.
    source: str
    # "" = the rule applied to anyone; otherwise the person it routed.
    actor: str


class RetireOut(Schema):
    """What retiring took with it. A retired runner cannot stay routed, so its
    routing rows are deleted — and said so here, because silently dropping a
    person's route sent their work to someone else's box with nothing surfacing it
    (2026-10-02)."""
    runner: str
    dropped_routes: list[DroppedRouteOut] = []


class RunnerAdminOut(Schema):
    """One explicit grant. No secret here — who, by whom, when."""

    user_id: int
    email: str
    granted_by_email: str = ""
    created_at: dt.datetime


class RunnerAdminIn(Schema):
    email: str


class RunnerMintOut(Schema):
    """A browser-driven re-authentication, as an OPERATOR sees it.

    Deliberately carries no `code`: the authorization code is the human's to type
    once, and echoing it back to a screen serves nothing and widens where it can
    leak. The minted token never appears in any operator-facing shape at all.
    """

    id: uuid.UUID
    status: str
    #: Which subscription login the new token will replace.
    slot: Literal["primary", "secondary"] = "primary"
    authorize_url: str
    detail: str
    created_at: dt.datetime
    updated_at: dt.datetime


class RunnerMintStartIn(Schema):
    slot: Literal["primary", "secondary"] = "primary"


class RunnerMintClaimOut(Schema):
    """What the RUNNER polls for: the work it owes, and the code when there is one.

    Null `mint` is the ordinary answer — no sign-in is in progress — so this is
    cheap to poll on the tick the runner already runs.
    """

    mint: RunnerMintOut | None = None
    code: str = ""


class RunnerMintUrlIn(Schema):
    url: str


class RunnerMintCodeIn(Schema):
    code: str


class RunnerMintResultIn(Schema):
    # Empty token = the attempt failed; `detail` says why, and is shown to the
    # human who is sitting there waiting for it.
    token: str = ""
    detail: str = ""


class RunnerCredentialStatusOut(Schema):
    """Masked view — booleans, never values. The POST response + any UI."""

    has_claude_token: bool = False
    has_claude_token_secondary: bool = False
    has_claude_api_key: bool = False
    claude_token_label: str = ""
    claude_token_secondary_label: str = ""
    updated_at: dt.datetime | None = None


class TurnGitHubTokenOut(Schema):
    """One turn's GitHub credential: its agent OWNER's token for that agent
    (`AgentDelegation`), and the identity to commit with. Handed to the runner
    that claimed the turn, for that turn's environment only."""

    token: str
    expires_at: dt.datetime | None = None
    github_login: str = ""
    git_name: str = ""
    git_email: str = ""
    #: `owner/repo` of the agent's own repo.
    repo: str = ""
    #: `Name <email>` of whoever caused the turn, for a `Requested-by:` trailer;
    #: "" when nobody in particular did (a schedule, a drill).
    requested_by: str = ""


class RunnerGitHubReadinessOut(Schema):
    """Can one agent this runner serves open a pull request, checked against
    GitHub at the moment of asking — so a missing or expired grant surfaces when
    the box boots, not in the middle of a turn."""

    agent_slug: str
    #: ok | warn (works, but expires within two weeks) | fail
    status: str
    detail: str = ""
    login: str = ""
    expires_at: dt.datetime | None = None


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
    #: The emdash project owning `session_key` — task names are unique per
    #: project only, so the runner aims the keystroke by (project, task).
    project: str = ""
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
    #: The emdash project owning `session_key`. The runner REFUSES a delete it
    #: cannot place in exactly one project rather than delete by name alone.
    project: str = ""


class CloseSyncOut(Schema):
    closes: list[CloseOut] = []


InvestigationStatus = Literal["open", "held", "debugger_failed", "resolved", "escalated"]


class FailureInvestigationOut(Schema):
    """One kind of turn failure and what the debugger agent did about it."""

    id: int
    workspace_slug: str
    fingerprint: str
    status: str
    normalized_note: str
    sample_note: str
    occurrences: int
    first_seen: dt.datetime
    last_seen: dt.datetime
    turn_ids: list[str]
    agents: list[str]
    runners: list[str]
    recurred_after_resolve: bool
    debug_turn_id: uuid.UUID | None
    triggers: int
    resolved_at: dt.datetime | None
    resolution_note: str

    @staticmethod
    def resolve_workspace_slug(obj) -> str:
        return obj.workspace_id


class ResolveInvestigationIn(Schema):
    note: str = Field(min_length=1, max_length=10000,
                      description="What was wrong and what you changed to fix it.")
