"""What each workspace role may DO — the one table, and the only door to it.

The roles and capabilities are defined, beside the agent roles they are often
confused with, in `docs/architecture/access.md`.

`services` answers "what is this person's role here?" (`membership`,
`member_role`), and `models.WorkspaceMembership.ROLE_RANK` orders the roles.
This module answers the question every gate actually asks: **may this person
do X in this workspace?** Code outside `apps/workspaces/` asks it here, by
capability, and never names a role — `tests/test_roles_named_only_in_workspaces.py`
fails the build on a role constant, a `member_role(...)` comparison, or a role
set anywhere else.

Why a table rather than `has_role_at_least(..., EDITOR)` at each call site: the
audit of 2026-10-02 found the same tier spelled five ways (`role in {editor,
owner}`, `!= OWNER`, `filter(role=OWNER)`, `_require_role(OWNER)`, rank), and
two of them silently changed meaning the moment a role was inserted between
editor and owner. With the table, adding or moving a role is one edit here,
and every gate follows.

Capabilities are named for what they PROTECT, not for a page, so one is
reused across surfaces that share a trust decision (every product surface's
writes are `CONTENT_WRITE`).
"""
from __future__ import annotations

from .models import WorkspaceMembership as _M

# --- membership -----------------------------------------------------------------

#: Read anything the workspace owns that is not a log: the board, agents, chats
#: you are in, product content. Talking to an agent and deciding its asks.
READ = "read"

# --- editor: making things --------------------------------------------------------

#: Create, change or delete product content: projects, walkthroughs,
#: shareouts, reviews, DDD runs and narratives, storyboards, issues, feedback
#: dispositions, contacts.
CONTENT_WRITE = "content.write"

#: Reshape an agent and direct its work: edit it, its skills, schedules, tasks
#: and routing; run turns; raise asks that dispatch work. NOT its keys — those
#: are the agent's admins' (`Agent.is_admin`) — and not `auto`: a turn an editor
#: who is not an agent admin starts always runs manual, and setting `auto` on a
#: dispatch, a routing rule or the agent's switch is an admin's (the editor tier,
#: docs/architecture/access.md).
AGENT_WORK = "agent.work"

#: Type into a runner-discovered emdash session (someone's live box).
SESSION_DRIVE = "session.drive"

#: Write to the workspace's event log (runners report alarms as their owner).
EVENTS_WRITE = "events.write"

# --- admin: running the workspace -----------------------------------------------

#: Read the workspace's LOGS: the event log, every turn's prompt, ledger, raw
#: transcript and caller context, stuck turns, runner drills, connected-site
#: health. Below admin you read the turns you started, and the chats you are in.
LOGS_READ = "logs.read"

#: Invite people, reissue and revoke invites, see invite links, and change or
#: remove members — always strictly BELOW your own role (`may_manage_member`).
MEMBERS_MANAGE = "members.manage"

#: The workspace's integrations: inbound mailboxes and push config, connected
#: sites (create, edit, test, read health), Slack history and sync.
INTEGRATIONS = "integrations"

#: The workspace's default runner order: which box takes repo turns, and every
#: agent here (and in workspaces below) that has no order of its own. Routing
#: work to boxes the workspace can already use — not a key (owner decision
#: 2026-10-05).
RUNNERS_ROUTE = "runners.route"

#: How long the CONTENT of this workspace's chats and turns is kept
#: (apps/retention). Admin, not owner, by owner decision (2026-10-05): it is
#: running the workspace, the same tier that already reads every turn's
#: content. Every change is written to the event log.
RETENTION_MANAGE = "retention.manage"

# --- owner: the keys --------------------------------------------------------------

#: Delete the workspace, nest or detach it, the shared vault, the Slack app
#: itself (config token, install, declare-an-agent), agents' credentials by
#: virtue of being every agent's admin, and transferring an agent's ownership.
#: A workspace ADMIN holds none of this: running a workspace is not holding its
#: agents' keys.
OWN = "own"

MINIMUM_ROLE: dict[str, str] = {
    READ: _M.VIEWER,
    CONTENT_WRITE: _M.EDITOR,
    AGENT_WORK: _M.EDITOR,
    SESSION_DRIVE: _M.EDITOR,
    EVENTS_WRITE: _M.EDITOR,
    LOGS_READ: _M.ADMIN,
    MEMBERS_MANAGE: _M.ADMIN,
    INTEGRATIONS: _M.ADMIN,
    RUNNERS_ROUTE: _M.ADMIN,
    RETENTION_MANAGE: _M.ADMIN,
    OWN: _M.OWNER,
}


def _rank(role: str | None) -> int:
    return _M.ROLE_RANK.get(role or "", -1)


def role_allows(role: str | None, capability: str) -> bool:
    """Does `role` hold `capability`? For a caller that already has the role in
    hand (a listing that read every membership once)."""
    return _rank(role) >= _M.ROLE_RANK[MINIMUM_ROLE[capability]]


def can(user, workspace, capability: str) -> bool:
    """May `user` exercise `capability` in `workspace` (a `Workspace` or slug)?
    False for a non-member, an anonymous user, or an unknown capability's
    KeyError — which is raised, not swallowed: a typo must not read as "no"."""
    from . import services

    if not getattr(user, "is_authenticated", False):
        return False
    minimum = MINIMUM_ROLE[capability]
    return services.has_role_at_least(user, workspace, minimum)


def slugs_with(user, capability: str, within: set[str] | None = None) -> set[str]:
    """The workspaces (of `within`, default all of the user's) where `user`
    holds `capability`. For listings and bulk writes that span tenants."""
    from . import services

    if not getattr(user, "is_authenticated", False):
        return set()
    # Every role in one read (`services.member_roles`), not a membership walk per
    # workspace — this runs on every runner's claim poll.
    roles = services.member_roles(user)
    candidates = within if within is not None else set(roles)
    return {s for s in candidates if role_allows(roles.get(s), capability)}


def request_slugs_with(request, capability: str) -> set[str]:
    """`slugs_with`, scoped like every request: a pinned `/api/w/{ws}/` call
    considers only that workspace."""
    from . import services

    return slugs_with(request.user, capability, within=services.request_workspace_slugs(request))


def is_owner(user, workspace) -> bool:
    """Shorthand for `can(user, workspace, OWN)` — the question listings of
    "who holds the keys" ask, e.g. `Agent.is_admin`."""
    return can(user, workspace, OWN)


def may_manage_member(actor_role: str | None, target_role: str | None,
                      new_role: str | None = None) -> bool:
    """May someone holding `actor_role` change a member holding `target_role`
    (to `new_role`, or remove them when `new_role` is None), or invite at
    `target_role`?

    An owner may do anything to anyone (the last-owner guard is separate, in
    `services`). Anyone else with MEMBERS_MANAGE acts only STRICTLY below
    themselves, and may only grant a role strictly below themselves — so an
    admin can invite, promote and remove viewers and editors, but can neither
    make another admin nor touch one, and nothing short of an owner can mint an
    owner.
    """
    if actor_role == _M.OWNER:
        return True
    if not role_allows(actor_role, MEMBERS_MANAGE):
        return False
    mine = _rank(actor_role)
    if target_role is not None and _rank(target_role) >= mine:
        return False
    if new_role is not None and _rank(new_role) >= mine:
        return False
    return True
