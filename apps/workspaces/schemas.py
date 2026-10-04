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
    # Deliberately no `self_join_domains` here: it is never client input.
    # `self_join_domains` grants standing DOMAIN-WIDE self-join eligibility
    # (every user of that domain may `POST /join` and become editor), so
    # letting a caller set it on their own workspace would let an attacker
    # declare an arbitrary allowlisted domain (e.g. "dimagi.com") and
    # silently recruit every teammate of that domain into their workspace.
    # Only `ensure_default_workspace()` may set it, straight from
    # `AUTH_ALLOWED_EMAIL_DOMAIN` server-side. `StrictModel`'s `extra="forbid"`
    # means a request that still sends this field is rejected (422), not
    # silently ignored — see the F1 security finding on the invite-aware
    # login gate.


class WorkspaceOut(StrictModel):
    slug: str
    display_name: str
    self_join_domains: list[str]
    role: str  # the requesting user's role in this workspace
    created_at: dt.datetime
    # The workspace directly above this one, or None for a root.
    parent: str | None = None
    # True when `role` comes from owning an ancestor rather than a membership
    # row here — the UI shows it so an org owner knows why they can see it.
    inherited: bool = False


class WorkspaceParentIn(StrictModel):
    """Move a workspace in the tree. `None` makes it a root."""

    parent: str | None = Field(default=None, max_length=64, pattern=SLUG_PATTERN)


class JoinableWorkspaceOut(StrictModel):
    """One workspace the caller may join by explicit action — a capability
    list, not a directory. `domain` is the entry of `self_join_domains` that
    matched, so the UI can say why ("your dimagi.com address is allowed")."""

    slug: str
    display_name: str
    domain: str


class MemberOut(StrictModel):
    user_id: int
    email: str
    role: str
    joined_at: dt.datetime
    # An owner of a PARENT workspace, listed because they own this one too.
    # There is no row here to change or remove — the grant lives on the parent.
    inherited: bool = False


class MemberRoleUpdateIn(StrictModel):
    role: Role


class InviteCreateIn(StrictModel):
    # EmailStr rejects a non-email string at the schema boundary, before it
    # ever becomes a matchable admission key for `pending_invite_for_email` /
    # `email_admitted_outside_domain` — an owner shouldn't be able to store
    # e.g. a garbage or wildcard-shaped value there.
    email: EmailStr = Field(max_length=200)
    role: Role = "editor"


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


class SharedVaultIn(StrictModel):
    """Non-clobbering on the KEY, exactly like AgentVaultIn: a blank or omitted
    service_key leaves the stored one alone, so renaming the vault does not
    silently wipe the credential that reads it."""

    vault: str | None = None
    service_key: str | None = None


class SharedVaultOut(StrictModel):
    """Masked. `key_set` is a boolean on purpose — this route never returns the
    key, and the only reader of the value is a runner that could actually run an
    agent in this workspace (GET /api/agents/{slug}/credentials/resolve)."""

    vault: str = ""
    key_set: bool = False
    # Set when this workspace has no vault of its own and uses an ancestor's
    # (Workspace.shared_vault_source). `vault`/`key_set` then describe THAT one,
    # so a division doesn't read as unconfigured when its agents are fine.
    inherited_from: str = ""


class TopologyRouteOut(StrictModel):
    """One `RunnerAssignment` row. `source` "" = the agent's default ordered list
    (`rank` orders it); non-empty = a source rule (optionally narrowed to one
    `actor`). `can_claim` is False when the runner's OWNER is not in the agent's
    workspace — the row routes on paper and the turn is never claimed."""

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
    without the agent's repo, and boxes whose owner cannot hold the agent."""

    workspace: str
    missing_repo: list[uuid.UUID]
    cannot_hold: list[uuid.UUID]


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
