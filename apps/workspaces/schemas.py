"""Pydantic schemas for the /api/workspaces surface."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import EmailStr, Field

from apps.common.schemas import StrictModel

from .models import SLUG_PATTERN

Role = Literal["owner", "admin", "editor", "viewer"]


class WorkspaceCreateIn(StrictModel):
    # Charset, not just length: the slug is an addressing token that ends up
    # inside the presence page key `<app>:<workspace>:<resource>` (parsed with
    # a bounded `split(":", 2)` over a resource segment that legitimately
    # contains colons). A colon-bearing slug is therefore irreducibly
    # ambiguous — `canopy:acme:eu:activity` reads as workspace `acme:eu` or as
    # workspace `acme` + resource `eu:activity` — which is a cross-tenant
    # roster leak the presence layer cannot undo. Single source of truth for
    # the pattern is `Workspace.SLUG_PATTERN`, which also validates the model
    # save path (a shell or management command never sees this schema).
    slug: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    # Nest the new workspace under an existing one. The caller must OWN the
    # parent (directly or by inheritance): creating a child grants the
    # parent's owners ownership of it, so this is an administrative act on the
    # parent, not on the new tenant.
    parent: str | None = Field(default=None, max_length=64, pattern=SLUG_PATTERN)
    # Deliberately no `access_request_domains` here: it is never client input.
    # It lets every user of a domain ask this workspace's admins for an
    # invitation (and, with `auto_approve_role` set, be let straight in), so a
    # caller setting it on their own workspace could declare "dimagi.com" and
    # solicit every Dimagi employee. Only `ensure_default_workspace()` (from
    # `AUTH_ALLOWED_EMAIL_DOMAIN`) and migrations set it. `StrictModel`'s
    # `extra="forbid"` rejects a request that still sends it (422) — see the
    # F1 security finding on the invite-aware login gate.


class WorkspaceOut(StrictModel):
    slug: str
    display_name: str
    # Login-email domains whose people may REQUEST an invitation (server-set).
    access_request_domains: list[str]
    # "" = off: a person approves every request. Else the role a request is
    # approved at automatically (viewer or editor).
    auto_approve_role: str = ""
    role: str  # the requesting user's role in this workspace
    created_at: dt.datetime
    # The workspace directly above this one, or None for a root.
    parent: str | None = None
    # The top of this workspace's tree — its organisation (itself for a root).
    # A caller whose workspaces have more than one `root` must name a workspace
    # on every list or search (the read rule, `services.read_scope`); the web
    # client reads this to know when to pin.
    root: str = ""
    # True when `role` comes from owning an ancestor rather than a membership
    # row here — the UI shows it so an org owner knows why they can see it.
    inherited: bool = False
    # A capability flag for clients: a list or search pinned to this workspace
    # also returns the caller's workspaces BELOW it (`services.pinned_read_scope`),
    # so one read pinned to a tree's root covers the organisation. Clients that
    # predate it fan a refused read out over each workspace instead.
    pinned_read_covers_descendants: bool = True


class WorkspaceParentIn(StrictModel):
    """Move a workspace in the tree. `None` makes it a root."""

    parent: str | None = Field(default=None, max_length=64, pattern=SLUG_PATTERN)


class RequestableWorkspaceOut(StrictModel):
    """One workspace the caller may request an invitation to — a capability
    list, not a directory. `domain` is the entry of `access_request_domains`
    that matched, so the UI can say why. `pending_request_id` is set when the
    caller already has an open request there."""

    slug: str
    display_name: str
    domain: str
    pending_request_id: int | None = None


AccessRequestStatus = Literal["pending", "approved", "denied"]
#: What an approval may grant. Owner is handed over on the Members list.
ApprovableRole = Literal["viewer", "editor", "admin"]
AutoApproveRole = Literal["", "viewer", "editor"]


class AccessRequestIn(StrictModel):
    note: str = Field(default="", max_length=1000)


class AccessRequestOut(StrictModel):
    id: int
    workspace: str
    workspace_display_name: str
    user_id: int
    email: str
    name: str
    note: str
    status: AccessRequestStatus
    # The role an approval granted ("" while pending or denied).
    role: str
    auto: bool
    decided_by_email: str | None = None
    decided_at: dt.datetime | None = None
    decision_reason: str = ""
    created_at: dt.datetime
    # The requester's role in the workspace NOW (None = not a member), so an
    # approved request's page can offer "change role" / "remove". Only filled
    # for admins' views.
    current_role: str | None = None
    # What happened when the admins were told — only on admins' views.
    notify_result: dict | None = None


class AccessRequestApproveIn(StrictModel):
    role: ApprovableRole = "viewer"


class AccessRequestDenyIn(StrictModel):
    reason: str = Field(default="", max_length=1000)


class AccessSettingsIn(StrictModel):
    """Owner-only. `auto_approve_role` "" turns auto-approval off."""

    auto_approve_role: AutoApproveRole


class MemberOut(StrictModel):
    user_id: int
    email: str
    role: str
    joined_at: dt.datetime
    # An owner of a PARENT workspace, listed because they own this one too.
    # There is no row here to change or remove — the grant lives on the parent.
    inherited: bool = False


class MemberAgentRef(StrictModel):
    slug: str
    name: str


class MemberWorkspaceRef(StrictModel):
    slug: str
    display_name: str
    role: str


class MemberAdminDetail(StrictModel):
    """What only someone who manages members sees about one (`members.manage`)."""

    invited_by_email: str | None = None
    last_login: dt.datetime | None = None
    # Chat sessions this member started in this workspace — a count, never the
    # sessions themselves: a chat's own sharing decides who may read it.
    chats_started: int = 0


class MemberProfileOut(StrictModel):
    """One member's page in a workspace, as the CALLER may see it. Every member
    sees who they are, their role and the agents they operate here; the rest is
    filled in only for a caller whose role allows it (`admin` is null below)."""

    user_id: int
    email: str
    name: str = ""
    role: str
    joined_at: dt.datetime
    inherited: bool = False
    # person | agent (an agent's own canopy login)
    kind: Literal["person", "agent"] = "person"
    is_you: bool = False
    # When `kind` is agent: the agent this login belongs to, in this workspace.
    agent: MemberAgentRef | None = None
    # Agents in this workspace this member operates (`Agent.owner`).
    operates: list[MemberAgentRef] = []
    # Other workspaces BOTH the caller and this member belong to, with this
    # member's role there — never a workspace the caller cannot already see.
    shared_workspaces: list[MemberWorkspaceRef] = []
    admin: MemberAdminDetail | None = None


class MemberRoleUpdateIn(StrictModel):
    role: Role


class MemberAddIn(StrictModel):
    # Someone already in this workspace's tree, added here directly — no invite.
    user_id: int
    role: Role


class MemberTreeWorkspaceOut(StrictModel):
    slug: str
    display_name: str
    parent: str | None = None
    # Below the tree's root (0 = the workspace the tree was asked for).
    depth: int
    # The caller's role here, and whether they may add, re-role or remove people.
    my_role: str
    can_manage: bool


class MemberTreeCellOut(StrictModel):
    role: str
    # Held by inheritance (an owner or admin of an ancestor): nothing here to
    # change — the grant lives on `inherited_from`.
    inherited: bool = False
    inherited_from: str | None = None


class MemberTreePersonOut(StrictModel):
    user_id: int
    email: str
    # {workspace slug: their role there}; a slug that is absent = not a member.
    cells: dict[str, MemberTreeCellOut]


class MemberTreeOut(StrictModel):
    root: str
    workspaces: list[MemberTreeWorkspaceOut]
    people: list[MemberTreePersonOut]


class InviteCreateIn(StrictModel):
    # EmailStr rejects a non-email string at the schema boundary, before it
    # ever becomes a matchable admission key for `pending_invite_for_email` /
    # `email_admitted_outside_domain` — an owner shouldn't be able to store
    # e.g. a garbage or wildcard-shaped value there.
    email: EmailStr = Field(max_length=200)
    # What accepting grants. Viewer unless the inviting admin picks otherwise.
    role: Role = "viewer"


InviteEmailStatus = Literal["sent", "throttled", "not_configured", "failed"]


class InviteOut(StrictModel):
    id: int
    email: str
    role: str
    token: str
    expires_at: dt.datetime
    accepted_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    invited_by_email: str | None = None
    last_emailed_at: dt.datetime | None = None
    # Set only on the create/reissue response: what happened to the email that
    # call tried to send. Absent from a listing, which sends nothing.
    email_status: InviteEmailStatus | None = None


InviteStatus = Literal["pending", "expired", "revoked", "accepted"]


class InvitePreviewOut(StrictModel):
    """Pre-auth invite preview — deliberately minimal disclosure. For any
    non-pending status, workspace_slug/workspace_display_name/role stay None:
    someone holding a dead token (forwarded, pasted into Slack, ...) learns
    only that it's dead, never which tenant it pointed at."""

    status: InviteStatus
    email_hint: str
    workspace_slug: str | None = None
    workspace_display_name: str | None = None
    role: str | None = None


class TopologyRouteOut(StrictModel):
    """One `RunnerAssignment` row. `source` "" = the agent's default ordered list
    (`rank` orders it); non-empty = a source rule (optionally narrowed to one
    `actor`). `can_claim` is False when the runner's OWNER is not in the agent's
    workspace, or the runner is directed-only — the row routes on paper and the
    turn is never claimed."""

    runner_id: uuid.UUID
    rank: int
    enabled: bool
    source: str
    actor: str
    strict: bool
    turn_mode: str
    can_claim: bool
    # True for a default-order row the agent FOLLOWS from its workspace's order
    # (it has none of its own), rather than a row of its own.
    inherited: bool = False


class TopologyFollowsOut(StrictModel):
    """An agent with no default order of its own follows `workspace`'s. Runners
    in that order the agent cannot use are listed here, not routed: laptops
    without the agent's repo, boxes whose owner cannot hold the agent, and
    directed-only boxes, which take no routed work."""

    workspace: str
    missing_repo: list[uuid.UUID]
    cannot_hold: list[uuid.UUID]
    directed: list[uuid.UUID] = []


class TopologyAgentOut(StrictModel):
    slug: str
    name: str
    turn_mode: str
    routes: list[TopologyRouteOut]
    # None = it has an order of its own, or nothing to follow.
    follows: TopologyFollowsOut | None = None
    repo_url: str = ""


class TopologyOrderRowOut(StrictModel):
    runner_id: uuid.UUID
    enabled: bool


class TopologyWorkspaceOut(StrictModel):
    slug: str
    display_name: str
    parent: str | None
    depth: int
    agents: list[TopologyAgentOut]
    # This workspace's own runner order (rank order). Empty = it follows
    # `order_from`, the nearest ancestor with one, or none at all.
    order: list[TopologyOrderRowOut] = []
    order_from: str | None = None


class RunnerProblemOut(StrictModel):
    name: str
    status: Literal["warn", "fail"]
    detail: str = ""


class RunnerAgentOut(StrictModel):
    slug: str
    name: str
    workspace: str


class TopologyRunnerOut(StrictModel):
    """A runner that lives in the tree or that an agent in it routes to.
    `in_tree` False = homed elsewhere (another workspace, or none)."""

    id: uuid.UUID
    name: str
    kind: str
    location: str
    status: str
    ready: bool
    ready_note: str
    paused: bool
    host: str
    last_heartbeat_at: dt.datetime | None
    workspace: str | None
    in_tree: bool
    owner_email: str | None
    flags: list[str]
    agent_count: int
    # Directed-only (`Runner.directed_only`): it takes turns pinned to it and
    # chats bound to it, never routed work — an order or list naming it is skipped.
    directed: bool = False
    # Running the code the deployed server expects for its kind? None = unknown
    # (either side never reported) — never read as stale.
    code_current: bool | None = None
    # Its own health checks that are not ok, as it last reported them.
    problems: list[RunnerProblemOut] = []
    # Why it is degraded / not ready, in its own words, and the fix when the
    # cause is a known one (`Runner.remedy`).
    status_note: str = ""
    remedy: str = ""
    code_sha: str = ""
    expected_code_sha: str = ""
    # The agents in the tree it serves.
    agents: list[RunnerAgentOut] = []
    # When the box last sent its usage report (`events.usage`).
    usage_reported_at: dt.datetime | None = None
    # The viewer administers it.
    can_administer: bool = False
    # The machine it is on (its instance role's name; "" = not known). Runners on
    # one machine are listed together.
    machine: str = ""
    # What it declares it serves: `tree:<root>`, `agent:<slug>`, or "" for none.
    unit: str = ""


class RunnerTopologyOut(StrictModel):
    """Workspaces in tree order (depth-first, `depth` 0 = the root)."""

    root: str
    workspaces: list[TopologyWorkspaceOut]
    runners: list[TopologyRunnerOut]


class AgentTopologyAgentOut(StrictModel):
    """`login_email` is the canopy user the agent signs in as (`Agent.user`) —
    the identity it sends other agents work with; None means it has none and can
    send nothing directly. `full_people` reach this agent's whole profile, and so
    reach anything this agent is an admin of."""

    slug: str
    name: str
    workspace: str
    owner_email: str | None
    login_email: str | None
    login_user_id: int | None
    interface_published: bool
    full_people: list[str]


class AgentEdgeOut(StrictModel):
    """What `source` gets when it sends `target` work with its own login.
    `access`: full | confined (to `capabilities`) | none. `basis`: owner |
    workspace-owner | admin | full-rule | editor (full, manual only) |
    no-interface (refused: nothing published) | capabilities | nothing-offered |
    not-member | no-login. Terms: docs/architecture/access.md. `can_grant` / `can_revoke`: whether
    THIS caller may make (or unmake) the source's login an admin of the target."""

    source: str
    target: str
    access: Literal["full", "confined", "none"]
    basis: str
    capabilities: list[str]
    full_rule: str | None
    explicit_admin: bool
    can_grant: bool
    can_revoke: bool


class TopologyWorkspaceRefOut(StrictModel):
    slug: str
    display_name: str
    depth: int


class AgentTopologyOut(StrictModel):
    """Agents in tree order; one edge per ordered pair of distinct agents."""

    root: str
    workspaces: list[TopologyWorkspaceRefOut]
    agents: list[AgentTopologyAgentOut]
    edges: list[AgentEdgeOut]


class RunnerOrderRowOut(StrictModel):
    """One row of a workspace's ordered runner list — the order REPO turns (project
    dispatches) in this workspace route by. `projects` is what the runner declares:
    a runner only ever takes a repo turn for a repo it lists there."""

    runner_id: uuid.UUID
    runner_name: str
    kind: str
    rank: int
    online: bool
    ready: bool
    enabled: bool
    projects: list[str]


class RunnerOrderRowIn(StrictModel):
    runner_id: uuid.UUID
    enabled: bool = True


class RunnerOrderIn(StrictModel):
    """Wholesale replace — index = rank. An empty list removes the order, and the
    workspace's repo turns go back to any runner that declares the repo."""

    runners: list[RunnerOrderRowIn]



# ---- workspace health (apps/agents/health.py) --------------------------------


class HealthStepLinkOut(StrictModel):
    href: str
    action: str


class HealthStepOut(StrictModel):
    key: str
    title: str
    status: Literal["done", "todo", "waiting", "optional"]
    detail: str
    where: Literal["web", "claude-code", "terminal"]
    href: str = ""
    action: str = ""
    command: str = ""
    links: list[HealthStepLinkOut] = []
    # Needed only for the agent to run on its own (the runner step) — see
    # apps/agents/setup.py::_step.
    only_to_run_on_its_own: bool = False


class VaultRefOut(StrictModel):
    name: str
    status: Literal["stored", "literal", "local", "1password", "missing"]


class AgentVaultHealthOut(StrictModel):
    source: str
    # canopy-web: every declared secret is stored here. moving: on canopy-web with
    # some still only in 1Password or nowhere. 1password: resolves through op.
    # none: declares no secrets and has no vault.
    state: Literal["canopy-web", "moving", "1password", "none"]
    declared: int
    refs: list[VaultRefOut]
    stored: list[str]
    in_1password: list[str]
    missing: list[str]
    op_vault: str
    op_key_set: bool
    zero_knowledge: bool
    machines_waiting: list[str]


class HealthSessionOut(StrictModel):
    id: str
    workspace: str
    title: str
    at: dt.datetime
    # Still open (`state=active`).
    active: bool


class AgentRunnerUseOut(StrictModel):
    name: str
    # cloud | emdash | … (`Runner.kind`), as it was when the turn ran.
    kind: str
    turns: int


class AgentHealthOut(StrictModel):
    slug: str
    name: str
    workspace: str
    owner_email: str
    # The viewer may follow this agent's fix links (its owner or one of its admins).
    can_fix: bool
    email: str
    repo_url: str
    # manual: its turns stop for a person's approval before acting or sending;
    # auto: they run through. (A routing rule can set a turn's mode itself.)
    turn_mode: str
    # Whether it publishes an interface: what people other than its admins may
    # ask it to do. False = only its admins (and editors) can use it.
    has_interface: bool
    # How many capabilities that interface offers (confined access).
    capabilities: int
    # Its `full:` rules: the caller classes that get the WHOLE agent, as its
    # admins do (e.g. "@dimagi.com:verified").
    full_access: list[str] = []
    skills: int
    schedules: int
    # Active projects; open tasks (suggested or in progress); open tasks waiting
    # on a person (an unanswered ask, or parked on someone).
    projects: int
    open_tasks: int
    waiting_tasks: int
    # Open sessions (the chat list's `state=active`) and when any of its sessions
    # last did something — over all its sessions.
    active_sessions: int
    last_session_at: dt.datetime | None = None
    # Its latest sessions, only those the viewer may open.
    recent_sessions: list[HealthSessionOut] = []
    created_at: dt.datetime
    last_turn_at: dt.datetime | None = None
    turns_30d: int
    # Over the same 30 days: turns that finished done, and failed or lost; the
    # people who did anything with it (asked it, chatted, messaged); and the
    # runners its finished turns ran on, most first.
    done_30d: int = 0
    failed_30d: int = 0
    people_30d: int = 0
    ran_on: list[AgentRunnerUseOut] = []
    # Its checklist and vault verdict; null when read with `setup=false`
    # (`AgentSetupHealthOut` carries them).
    vault: AgentVaultHealthOut | None = None
    steps: list[HealthStepOut] | None = None
    # Steps neither done nor optional.
    left: int | None = None


class AgentSetupHealthOut(StrictModel):
    slug: str
    vault: AgentVaultHealthOut
    steps: list[HealthStepOut]
    # Steps neither done nor optional.
    left: int


class WorkspaceHealthSetupOut(StrictModel):
    agents: list[AgentSetupHealthOut]


class WorkspaceHealthNodeOut(StrictModel):
    slug: str
    name: str
    depth: int
    parent: str
    agents: int


class HealthTotalsOut(StrictModel):
    agents: int
    # Counted from the checklists: null when read with `setup=false`.
    set_up: int | None = None
    needs_setup: int | None = None
    # Agents anyone used in the last 30 days (a turn, or a person's chat or message).
    used_30d: int = 0
    vault_canopy_web: int | None = None
    vault_moving: int | None = None
    vault_1password: int | None = None
    vault_none: int | None = None
    runners: int
    runners_online: int


class WorkspaceHealthOut(StrictModel):
    root: str
    workspaces: list[WorkspaceHealthNodeOut]
    agents: list[AgentHealthOut]
    # The runners the tree's agents route to, or that live in it — the runner
    # topology's own rows, plus each box's usage report.
    runners: list[TopologyRunnerOut]
    totals: HealthTotalsOut


# ---- workspace analytics (apps/events/analytics.py) ---------------------------


class AnalyticsTotalsOut(StrictModel):
    turns: int
    sessions: int
    messages: int
    active_users: int
    active_agents: int
    # Repositories with work no agent ran: repo turns, people's own sessions.
    active_repositories: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    # Claude Code sessions whose boxes reported their tokens (`events.usage`).
    sessions_with_usage: int


class AnalyticsDayOut(StrictModel):
    day: dt.date
    turns: int
    sessions: int
    messages: int
    active_users: int
    done: int
    failed: int
    lost: int
    cancelled: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


class AnalyticsRowOut(StrictModel):
    """One row of a breakdown — the same figures for an agent, a repository and a
    person (`analytics._breakdown`)."""

    turns: int
    # Of those, finished failed or lost.
    failed: int
    sessions: int
    messages: int
    settings: int
    mcp_calls: int
    people: int
    active_days: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    last_at: dt.datetime | None = None


class AnalyticsAgentOut(AnalyticsRowOut):
    agent: str


class AnalyticsRepositoryOut(AnalyticsRowOut):
    repository: str


class AnalyticsUserOut(AnalyticsRowOut):
    user_id: int
    email: str
    # What they worked with most: agents' slugs, else repositories' names.
    top: list[str]


class NetworkNodeOut(StrictModel):
    """A person, a contact, an agent, or a thread (a chat — where a bound Slack thread
    lives too — or an email thread)."""

    id: str
    kind: str  # person | contact | agent | thread
    label: str
    # Where it lives: a thread's workspace, an agent's home; a person's or contact's
    # first thread's. `workspaces` is every workspace it appears in.
    workspace: str
    workspaces: list[str]
    turns: int
    threads: int
    # A thread's channel: chat, slack, email, api, ace-web, scheduler.
    channel: str = ""
    # A thread's session or mail-thread id; an agent's slug. Blank otherwise.
    ref: str = ""
    # A thread's answering agent.
    agent: str = ""
    # Whether the viewer may open the thread (the session ACL for a chat).
    openable: bool = False
    # A thread's latest turn in the window.
    last_at: dt.datetime | None = None


class NetworkLinkOut(StrictModel):
    source: str
    target: str
    turns: int
    kind: str  # wrote (someone wrote in a thread) | answered (the agent answering it)


class NetworkTotalsOut(StrictModel):
    people: int
    contacts: int
    agents: int
    threads: int
    # Pairs of people or contacts who wrote in the same thread.
    shared_pairs: int
    # Threads more than one person or contact wrote in.
    shared_threads: int
    # Every thread in the window, drawn or not (`truncated`).
    threads_seen: int


class WorkspaceNetworkOut(StrictModel):
    days: int
    since: dt.datetime
    workspaces: list[str]
    nodes: list[NetworkNodeOut]
    links: list[NetworkLinkOut]
    totals: NetworkTotalsOut
    truncated: bool


class WorkspaceAnalyticsOut(StrictModel):
    days: int
    since: dt.datetime
    workspaces: list[str]
    totals: AnalyticsTotalsOut
    series: list[AnalyticsDayOut]
    surfaces: dict[str, int]
    agents: list[AnalyticsAgentOut]
    repositories: list[AnalyticsRepositoryOut]
    users: list[AnalyticsUserOut]
