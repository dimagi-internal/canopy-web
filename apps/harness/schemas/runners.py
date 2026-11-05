"""Runner registry, health, flags, projects, holds and heartbeat schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Literal

from canopy_sdk import contract
from ninja import Schema
from pydantic import Field

from .. import clis as cli_vocab
from .. import runner_refresh
from ..runner_requirements import is_sensitive_data_box


class OpenSessionReason(Schema):
    count: int
    oldest_activity_at: dt.datetime | None = None


class RunnerOpenSessionStarter(Schema):
    """Who started a session: `kind` is `initiator.KINDS` (user, contact, system,
    agent). An agent's own login is reported as `agent`, with its slug."""

    kind: str
    agent: str = ""
    email: str = ""
    name: str = ""


class RunnerOpenSessionOut(Schema):
    """One open session on a runner, for the box's owner and admins: facts only —
    no title, prompt or message (`apps/harness/runner_sessions.py`)."""

    id: uuid.UUID
    agent: str = ""
    workspace: str
    origin: str
    open_reason: str
    started_by: RunnerOpenSessionStarter | None = None
    first_turn_origin: str = ""
    first_turn_mode: str = ""
    turns: int = 0
    question_waiting: bool = False
    transferred_at: dt.datetime | None = None
    sensitive: bool = False
    created_at: dt.datetime
    last_activity_at: dt.datetime | None = None


class RunnerOpenSessionCloseOut(Schema):
    ok: bool
    closing: bool = False
    reason: str = ""


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
    # does, but no runner is reachable right now (usually transient); "parked" =
    # every runner that could take it is paused (`reason` says by whom, until when);
    # "hold" = the fleet is on hold; "agent_hold" = its agent is (Agent.held);
    # "workspace_hold" = its workspace, or one above it, is (Workspace.held).
    kind: str = "config"
    # Whether the caller may cancel it (POST /turns/{id}/cancel) — the fix the
    # warning offers in place.
    can_cancel: bool = False


class SlashCommandIn(Schema):
    """One command a Claude session on the box can run, as `/name args`."""

    name: str = Field(max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    description: str = Field(default="", max_length=300)
    args_hint: str = Field(default="", max_length=120)
    # Where the box found it: user (its account's own), plugin, acp (announced).
    source: str = Field(default="", max_length=16)
    # `user-invocable: false` in its file: a real skill Claude may load, not one to
    # offer a person. A report from an older runner lacks it, which means not internal.
    internal: bool = False
    # When Claude Code last updated the plugin whose file this is, on this box: which
    # box has the newest copy (`agents.skill_catalog`). Blank from an older runner.
    plugin_updated_at: str = Field(default="", max_length=40)


class SlashCommandsIn(Schema):
    # The whole list, replacing what was held — the box sends it when it changes.
    commands: list[SlashCommandIn] = Field(default_factory=list, max_length=1000)


class RunnerCapabilitiesIn(Schema):
    # Wholesale replacement, like the skill catalog — the caller sends the full
    # capabilities it wants (e.g. {"agents": [...], "projects": ["canopy-web"]}).
    capabilities: dict


class HealthCheck(Schema):
    """One feature a runner checked on itself. `warn` is "works, but a person
    should know" (a CLI behind its plugin); `fail` is "this feature
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
    # The box's canopy CLI version ("" = unknown), compared with the canopy
    # release to ask a stale box for a refresh (`canopy_release.refresh_owed`).
    canopy_version: str = Field(default="", max_length=32)
    # The earliest the box will restart itself again (its floor between
    # self-restarts) — why a requested refresh is waiting when nothing is busy.
    refresh_floor_until: float = 0
    # Why the box is not asking for work just now, while it restarts or replaces a
    # tool ("" = it is, or something else stops it): `Runner.setting_up`.
    setting_up: str = Field(default="", max_length=200)


class RefreshChangeOut(Schema):
    name: str
    before: str = ""
    after: str = ""


class RunnerRefreshOut(Schema):
    """Where this runner's Refresh is (`runner_refresh.status`): `none`, `pending`
    with what it waits on (`offline`, `busy`, `floor`; empty = its next
    heartbeat), or `done` with what changed since it was asked."""

    state: Literal["none", "pending", "done"]
    requested_at: dt.datetime | None = None
    requested_by: str = ""
    waiting_on: list[Literal["offline", "busy", "floor"]] = []
    in_flight: int = 0
    eligible_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    took_seconds: int | None = None
    changes: list[RefreshChangeOut] = []
    problems: list[str] = []


class RunnerFlagsIn(Schema):
    flags: list[str]


class RunnerPrivateIn(Schema):
    private: bool


class RunnerOnlyAgentsIn(Schema):
    # Agent slugs; [] = no restriction (the box runs whatever routes to it).
    agents: list[str]


class RunnerDirectedIn(Schema):
    directed: bool


class RunnerNameIn(Schema):
    name: str = Field(min_length=1, max_length=100)


class RunnerAgentChoiceOut(Schema):
    slug: str
    name: str
    workspace: str


class RunnerOnlyAgentsOut(Schema):
    # The agents the box is dedicated to now; [] = no restriction.
    agents: list[str]
    # Every agent it could be dedicated to: in the workspaces it serves and visible to you.
    choices: list[RunnerAgentChoiceOut]


class RunnerEgressIn(Schema):
    # Locked: the machine's runners reach the network only through its proxy, to the base
    # set (canopy-web, the model, GitHub) plus `allow`. A sensitive-data box cannot be unlocked.
    locked: bool
    # Extra hostnames; `.example.com` admits every subdomain. Replaces the list.
    allow: list[str] = []


class BoxEgressIn(Schema):
    # An `X-Canopy-Aws-Proof` value for this URL: the box's instance role.
    proof: str


class BoxEgressRunnerOut(Schema):
    id: uuid.UUID
    name: str
    locked: bool
    allow: list[str]


class BoxEgressOut(Schema):
    # Every live runner of the box's instance role, as its egress is set.
    runners: list[BoxEgressRunnerOut]


class RunnerEngineIn(Schema):
    # emdash | claude-desktop | claude-cli (Runner.ENGINE_CHOICES). Validated in the view so the
    # error names the choices rather than a pydantic literal mismatch.
    engine: str


class RunnerProjectAskOut(Schema):
    """One repo a laptop owes: make it the emdash project `name`. `requested_at`
    changes when the ask is re-armed, so a box that already answered the old one
    knows this is a new attempt."""

    id: uuid.UUID
    name: str
    repo_url: str
    requested_at: dt.datetime


class RunnerProjectRequestOut(Schema):
    """An ask to add a repo to a laptop runner, and how it went: `pending` until
    the box adds it (or its heartbeat reports the project), `added`, or `failed`
    with the box's reason in `detail`."""

    id: uuid.UUID
    runner_id: uuid.UUID
    name: str
    repo_url: str
    agent: str | None = None
    status: str
    detail: str = ""
    requested_at: dt.datetime
    finished_at: dt.datetime | None = None
    requested_by_email: str | None = None

    @staticmethod
    def resolve_agent(obj) -> str | None:
        return obj.agent.slug if obj.agent_id else None

    @staticmethod
    def resolve_requested_by_email(obj) -> str | None:
        return obj.requested_by.email if obj.requested_by_id else None


class RunnerProjectsOut(Schema):
    """What emdash projects a laptop holds (as it reports them), what it was asked
    to add, and whether the caller may ask it (its owner or an administrator, on a
    laptop — a cloud box clones its agents on its own)."""

    projects: list[str]
    requests: list[RunnerProjectRequestOut]
    can_add: bool


class RunnerProjectRequestIn(Schema):
    """Add a repo: name an `agent` (its repo, under its slug) or give `repo_url`
    (and optionally `name`, else the repo's own name)."""

    agent: str = ""
    repo_url: str = ""
    name: str = ""


class RunnerProjectResultIn(Schema):
    """The box's word on one ask: whether emdash now holds the project, and why not."""

    ok: bool
    detail: str = ""


def _enrolled_roots(obj) -> list[str]:
    """`runners.enrolled_roots`, read once per row."""
    cached = getattr(obj, "_enrolled_roots_cache", None)
    if cached is None:
        from ..runners import enrolled_roots

        cached = enrolled_roots(obj)
        obj._enrolled_roots_cache = cached
    return cached


class RunnerCliOut(Schema):
    """One agent CLI on a box, as the server reads its report (`clis.reported`)."""

    label: str
    signed_in: bool
    # It enforces the current guard and caller-envelope versions for this CLI, so a
    # caller's (confined) turn may run on it (`clis.confines`).
    confines: bool
    version: str = ""
    # A CLI this server does not know (a newer runner): never routed to.
    known: bool = True


class RunnerOut(Schema):
    id: uuid.UUID
    name: str
    # The name the box paired as; differs from `name` once a person renames it.
    paired_as: str = ""
    kind: str
    # The session runtime this (laptop) runner opens NEW sessions in — emdash,
    # claude-desktop or claude-cli. The runner reads it off its own heartbeat response, so this
    # field is also how a flip reaches the box (canopy-web#1188).
    engine: str = "emdash"
    # The OS the box said it runs when it paired (macos, windows, linux); "" unknown.
    platform: str = ""
    status: str
    status_note: str
    ready: bool
    ready_note: str
    # What to do about a degraded / not-ready box when the note names a known
    # cause (`Runner.remedy`); "" otherwise. The menu bar shows it.
    remedy: str = ""
    # Served alongside `status` (which already reads "paused" via live_status)
    # because the two answer different questions: `status` says the box is not
    # taking work, these say a HUMAN decided that and why. A client that only
    # renders status still behaves correctly — that is the point of deriving it
    # in live_status — but it cannot explain itself without these.
    paused: bool
    paused_note: str
    paused_at: dt.datetime | None
    paused_by_email: str | None = None
    # The scheduled unpause, if one is set (a usage cap's reset); null = paused
    # until a human unpauses.
    unpause_at: dt.datetime | None = None
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
    # The canopy release (plugin + CLI) the box should run — the second version
    # it runs, beside its code (`canopy_release`). The box compares its CLI with
    # it in its `canopy.cli` check. "" = unknown.
    expected_canopy_version: str = ""

    workspace: str | None
    # The runner's OWNER — the human whose token it authenticates with. This — NOT `workspace` — is what governs
    # what the runner may WORK FOR (claim_next_turn derives the tenant from the
    # owner's workspace memberships, so a runner serves agents across every
    # workspace its owner belongs to). `workspace` is only the home/visibility
    # tenant. Surfaced so the supervisor can show the meaningful owner instead of
    # implying a single-workspace serving scope.
    owner_email: str | None
    # Whether THIS caller may mutate the runner (declare capabilities, retire,
    # heartbeat/claim as it) — a property of the (caller, runner) pair, so
    # list_runners stamps it on each row; a Ninja resolver only sees the row.
    #
    # Defaults True because every OTHER route returning a RunnerOut resolves its
    # runner through `runner_or_404`, which IS the act-on gate — reaching one of
    # those responses at all proves the caller can manage it. Only the list can
    # legitimately contain a runner the caller may not act on, and only the list
    # sets this per row.
    can_manage: bool = True
    # Whether the caller may ADMINISTER this box (credentials, browser sign-in)
    # as distinct from speaking AS it (drills, pause, claim). Separate flags
    # because they gate different routes: reporting one for the other is how a
    # UI ends up rendering a control that 404s.
    can_administer: bool = True
    # {repo: the workspaces YOU can start a repo chat in on this box}: its owner
    # assigned the repo there, and the workspace's runner order lists the box or you
    # administer it. Stamped by the fleet list; what the New chat menu offers.
    repo_workspaces: dict[str, list[str]] = {}
    # How many sessions are open on this box right now (active, and still being
    # reported — the same rule as the session list's `state=active`). Stamped by
    # the fleet list only; 0 elsewhere. Supervisor → Runners shows it beside a
    # pause ("paused · 4 open sessions"): idle sessions on a paused box are not
    # waiting on anybody, so they are counted here rather than on the feed.
    open_sessions: int = 0
    # The same open sessions by WHY each is open (`staleness.OPEN_REASONS`, plus
    # `unclassified` if the rule and its explanation ever disagree), each with the
    # oldest last activity among them. Counts only: no session, title or person,
    # so it is shown to anyone who sees the runner. Stamped by the fleet list and
    # the one-runner read; None on a route that does not count.
    open_sessions_by_reason: dict[str, OpenSessionReason] | None = None
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

    # What this box's owner has declared about it (RunnerFlag) — e.g. `sensitive_data`.
    flags: list[str] = []
    # Its owner's own machine: no fleet work, only what its owner puts on it.
    private: bool = False
    # The private-runner registration linked to this box (canopy-web#1705): the runner
    # writes it to ~/.canopy/private-runner.json, so direct sessions there need no CLI.
    private_runner_id: uuid.UUID | None = None
    # The agents a person dedicated this box to (`Runner.only_agents`): it runs only
    # their turns and chats. [] = no restriction.
    only_agents: list[str] = []
    # Whether it takes only what is put on it (`Runner.directed_only`): a person's
    # setting, and always for a private runner.
    directed: bool = False
    # The egress lock a person set (`PUT …/egress`); `egress_forced`: a sensitive-data box,
    # locked whatever is set. Root on the box applies it within a minute.
    egress_locked: bool = False
    egress_allow: list[str] = []
    egress_forced: bool = False
    # A fleet member (`apps/harness/fleets.py`): its fleet's name and its EC2
    # instance ("" for any other runner), and where the scaler has it — on /
    # draining / stopped (in the warm pool) / waking.
    fleet_name: str = ""
    instance_id: str = ""
    power: str = "on"
    power_changed_at: dt.datetime | None = None

    # The machine this runner is on (`Runner.machine_name`): several runners can
    # share one. "" when not known — a laptop names itself in `host`.
    machine: str = ""

    # The roots of the workspace trees the box is enrolled in, as far as the
    # caller may know them: all of them for the box's owner, else only the trees
    # the caller is a member somewhere in. Stamped per (caller, runner) by
    # `api._with_roots`, which every route returning a RunnerOut calls; an
    # unstamped row serves [] (the heartbeat reply, which only the box reads).
    # A cloud runner names one tree (`runners._other_tree`); a laptop may name several.
    roots: list[str] = []
    # Enrolled in more than one tree, whether or not the caller sees them all: on
    # that machine nothing separates the trees from each other.
    spans_trees: bool = False
    # Where to approve a key on this box that waits for a person — a new key, or an
    # active one whose new ECDH key does — or "" when none waits. Stamped on the
    # heartbeat reply only (`api_device_keys.waiting_approval_url`), so the box's
    # menu bar can say so; the email and push about it are easy to miss.
    key_waiting: str = ""
    # The repos whose sessions the box should report (`session_sync.report_projects`):
    # null = every repo. Stamped on the heartbeat reply only; a box filters its
    # session report by it before reading any transcript tail.
    report_projects: list[str] | None = None

    # The agent CLIs the box has, keyed by CLI, as routing reads them: a box that
    # reports none is Claude Code alone, signed in unless it said otherwise. A map
    # for the reason `health_checks` is one.
    agent_clis: dict[str, RunnerCliOut] = {}

    @staticmethod
    def resolve_agent_clis(obj) -> dict[str, RunnerCliOut]:
        if not hasattr(obj, "capabilities"):
            return {}
        out = {cli: RunnerCliOut(label=cli_vocab.label(cli), signed_in=r.signed_in,
                                 confines=cli_vocab.confines(r), version=r.version)
               for cli, r in cli_vocab.reported(obj).items()}
        for name in (obj.capabilities or {}).get("clis_unknown") or []:
            out[str(name)] = RunnerCliOut(label=str(name), signed_in=False, confines=False,
                                          known=False)
        return out

    @staticmethod
    def resolve_private_runner_id(obj) -> uuid.UUID | None:
        try:
            pr = obj.private_runner
        except Exception:  # noqa: BLE001 - the reverse one-to-one is absent
            return None
        return pr.pk if pr is not None and pr.retired_at is None else None

    @staticmethod
    def resolve_directed(obj) -> bool:
        return obj.directed_only()

    @staticmethod
    def resolve_fleet_name(obj) -> str:
        # Only a member reads its fleet (one query each); every other runner is "".
        return obj.fleet.name if getattr(obj, "fleet_id", None) else ""

    @staticmethod
    def resolve_only_agents(obj) -> list[str]:
        return sorted(obj.only_agent_slugs())

    @staticmethod
    def resolve_egress_forced(obj) -> bool:
        return is_sensitive_data_box(obj)

    @staticmethod
    def resolve_key_waiting(obj) -> str:
        return getattr(obj, "key_waiting_url", "") or ""

    @staticmethod
    def resolve_report_projects(obj) -> list[str] | None:
        return getattr(obj, "report_projects_wanted", None)

    @staticmethod
    def resolve_roots(obj) -> list[str]:
        return list(getattr(obj, "visible_roots", None) or [])

    @staticmethod
    def resolve_machine(obj) -> str:
        return obj.machine_name() if hasattr(obj, "machine_id") else ""

    @staticmethod
    def resolve_spans_trees(obj) -> bool:
        return len(_enrolled_roots(obj)) > 1

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
    # The same request as a person reads it — what it waits on, or what it changed.
    refresh: RunnerRefreshOut | None = None
    # Turns this runner reported running that a person asked to stop — set only
    # on a heartbeat reply (services.cancel_requested_turn_ids), empty elsewhere.
    # The durable half of a stop: the `cancel` frame is the doorbell, this is
    # what reaches a runner whose socket missed it.
    cancel_turn_ids: list[str] | None = None
    # Repos a person asked this laptop to make emdash projects of
    # (`runner_projects.owed`) — set only on a heartbeat reply, None elsewhere.
    # The durable ask; the box reports each back on `…/projects/{id}/result`.
    add_projects: list[RunnerProjectAskOut] | None = None

    @staticmethod
    def resolve_add_projects(obj) -> list[RunnerProjectAskOut] | None:
        wanted = getattr(obj, "add_projects_wanted", None)
        if wanted is None:
            return None
        return [RunnerProjectAskOut(id=r.pk, name=r.name, repo_url=r.repo_url, requested_at=r.requested_at)
                for r in wanted]

    @staticmethod
    def resolve_health_checks(obj) -> dict[str, HealthCheck] | None:
        if not obj.health:
            return None
        checks = {c["name"]: HealthCheck(**c) for c in obj.health.get("checks") or []}
        # `github.<slug>` is canopy-web's own delegation state, which the box only
        # asks about at boot. Served from the box's report it stayed red for a
        # token lent after that boot, under a fresh `health_received_at` (eva,
        # 2026-10-03). So it is answered here, from the rows, on every read.
        from apps.agents.credentials import delegations

        from ..routing import agents_routed_to

        checks = {n: c for n, c in checks.items() if n != "github" and not n.startswith("github.")}
        for agent in agents_routed_to(obj):
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
    def resolve_refresh(obj) -> RunnerRefreshOut | None:
        if not hasattr(obj, "refresh_requested_at"):
            return None
        return RunnerRefreshOut(**asdict(runner_refresh.status(obj)))

    @staticmethod
    def resolve_cancel_turn_ids(obj) -> list[str] | None:
        return list(getattr(obj, "cancel_turn_ids", None) or [])

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
        return obj.expected_code_committed_at()

    @staticmethod
    def resolve_expected_canopy_version(obj) -> str:
        return obj.expected_canopy_version()

    @staticmethod
    def resolve_workspace(obj) -> str | None:
        return obj.workspace_id

    @staticmethod
    def resolve_owner_email(obj) -> str | None:
        return obj.owner.email if obj.owner_id else None

    @staticmethod
    def resolve_paused_by_email(obj) -> str | None:
        return obj.paused_by.email if obj.paused_by_id else None

    @staticmethod
    def resolve_status(obj) -> str:
        # Serve the derived value, not the stored column: heartbeat() writes
        # ONLINE and nothing ever demotes it, so the raw status lies once a
        # runner goes quiet. See Runner.live_status.
        return obj.live_status


class FleetHoldIn(Schema):
    # What you are tracing, so whoever finds the fleet silent knows why and when to
    # release it. Same argument as PauseIn's note, at fleet scale.
    note: str = ""


class FleetHoldOut(Schema):
    held: bool
    note: str
    held_at: dt.datetime | None = None
    held_by_email: str = ""
    # Turns waiting on the hold right now — the "what is trying to start" count.
    queued: int = 0
    # Whether the CALLER may hold (a superuser, or a named holder such as Ada) and
    # release (a superuser only). The UI offers each control only on its flag, so
    # nobody is handed a button that 403s.
    can_hold: bool = False
    can_release: bool = False
    # Whether the caller may use /fleet-stop: a superuser only, like the release —
    # it cuts people's work off mid-turn, so a named holder (an agent) may not.
    can_stop_running: bool = False


class WorkspaceHoldOut(Schema):
    # This workspace's own hold — or, when `held_by_workspace` names an ancestor,
    # that ancestor's, which covers this one too (only its holder can release it).
    workspace: str
    held: bool
    held_by_workspace: str = ""
    note: str = ""
    held_at: dt.datetime | None = None
    held_by_email: str = ""
    # Turns in this workspace (and below) waiting on the hold right now.
    queued: int = 0
    # Whether the caller holds `control` here: an owner or a superuser.
    can_control: bool = False


class FleetStopOut(Schema):
    # What one /fleet-stop reached: sessions the stop interrupted, and executing
    # turns no session stop had reached that were told to cancel.
    stopped_sessions: int
    stopped_turns: int


class PauseIn(Schema):
    # Why this box is parked, for whoever finds it idle later. A pause with no
    # reason is indistinguishable from a broken runner at a glance, and the
    # cost of THIS feature going wrong is a box that stays silent long after
    # the reason expired.
    note: str = ""
    # Schedule the unpause for this time — what a runner sends when its Claude
    # subscription is capped, with the reset the CLI named. Omitted = an operator
    # pause that holds until /unpause. Never shortens a scheduled unpause or puts
    # one on an operator's open-ended pause (see `pause_runner`).
    until: dt.datetime | None = None


class HeartbeatIn(Schema):
    active_turn_ids: list[str] = []
    degraded: bool = False
    note: str = ""
    host: str = ""  # refresh the owning macOS host (in case a runner row is reused)
    # "macos" | "windows" | "linux" (browser_pair.platform_name). Keeps
    # Runner.platform true after pairing — the Windows remedy text reads it. Absent
    # (older code) or anything else leaves the stored value alone.
    platform: str = ""
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
    # given a CALLER's confined turn (services.profile_q).
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
    # Which agent CLIs this box has, signed in and confining (`clis.sanitize`):
    # {"codex": {"signed_in": true, "profiles": 3, "envelope": 2, "version": "…"}}.
    # A level like `profiles`: absent (older code) means the box is read as Claude
    # Code alone (`clis.reported`).
    clis: dict | None = None
    # The Claude model this box's sessions start on, as its settings name it
    # (`opus[1m]`): the only place the 1M-window `[1m]` is written down — the
    # transcript records the bare API id. The chat's context meter sizes the
    # window from it. Absent (older code, the cloud runner) means "".
    claude_model: str = ""
    # The box's data mode (`RUNNER_DATA_MODE`; "sensitive_data" = one ZDR API key, a
    # locked profile on every turn). A person's `sensitive_data` declaration counts
    # only while this says "sensitive_data" (`runner_requirements.in_force`). Absent = "".
    data_mode: str = ""
    # A hash of the config that mode produced, so a change on the box is visible.
    data_mode_hash: str = ""
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
    # The agents this box TRIED to set up and could not (it has no clone of
    # them). Same absent-is-not-empty rule: None = not reported (an older box,
    # or one that has not bootstrapped yet) and leaves the stored list alone;
    # [] = "every agent I tried is set up". The claim gives such a box none of
    # those agents' turns (routing.cannot_serve).
    agents_unprepared: list[str] | None = Field(default=None, max_length=500)
