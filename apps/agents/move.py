"""Moving an agent to another workspace — with its history (#1532).

`Agent.workspace` is one pointer, but an agent's history lives in many tables,
and several of them carry their OWN `workspace` FK: chat sessions, capability
grants and leases, agent-scoped admission and retention rules, origin issues,
and (in the mail app) threads, messages and routes. Changing the pointer alone
left all of that in the old tenant, where it was invisible from the new one or
404'd through the by-slug gates. A move is therefore one operation, `move()`,
that re-homes every agent-scoped row in ONE transaction or changes nothing.

**One code path for the dry run and the move.** Every table is handled by a
participant that both COUNTS and (when applying) WRITES, so what a dry run says
is what the move does — they cannot drift, because there is no second
implementation to drift.

**Participants are a registry** because some of the tables belong to product
apps (`mail`) and this module is framework tier: it may not import them. A
product app registers its participant from its `AppConfig.ready()`
(`apps/mail/apps.py`), exactly as the timeline registry lets product sources in.

What does NOT move, deliberately, and is reported as `left_behind`:
- **People records** (`contacts.PersonFact` the agent asserted, `Contact`) are
  the workspace's knowledge about people, not the agent's history. Copying them
  would put a person's facts in a tenant they never told anything; moving them
  would strip the old tenant of what it knows. They stay.
- **A person's grants** (`contacts.PersonGrant`) reach one workspace's entries,
  by the person's own act (HCP 4.1.6). Re-pointing one at a different
  workspace is a grant nobody gave, so they stay and stop applying (lookups key
  on `agent.workspace`); the person grants again in the new tenant.
- **Audit** (`events.Event`, `SettingChange`) stays where it happened.

What REFUSES the move: a workspace project the agent stewards. It is the
workspace's and stays there, and its steward must be in its workspace or one above it, so the
move waits until each one is handed to another steward.

What follows the agent with no write at all — tasks, projects, skills,
schedules, runner assignments, turns (whose tenancy derives from
`agent.workspace`), session messages — is still COUNTED, so the dry run shows
the whole of what moves.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Callable

from django.db import IntegrityError, transaction

from apps.admission.models import AdmissionRule
from apps.agents.services import runner_may_hold_agent, stewarded_workspace_projects
from apps.canopy_sessions import filing
from apps.canopy_sessions.models import Message, Session
from apps.capabilities.models import CapabilityGrant, CapabilityLease
from apps.contacts.models import PersonFact, PersonGrant
from apps.events import services as events
from apps.harness import routing
from apps.harness.models import AgentSchedule, RunnerAssignment, Turn
from apps.issues.models import OriginIssue
from apps.retention.models import RetentionRule
from apps.slack.services import installation_for_workspace
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from .agent_login import follow_agent
from .models import Agent, AgentAdmin, AgentProject, AgentSkill, AgentTask

logger = logging.getLogger(__name__)


class MoveError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Plan:
    """What a move carries, leaves, and breaks. Filled by the participants."""

    agent: object
    source: Workspace
    dest: Workspace
    by: object
    #: [{"table", "rows", "how"}] — every agent-scoped table, moved or following.
    tables: list[dict] = field(default_factory=list)
    #: [{"table", "rows", "why"}] — rows deliberately left in the source.
    left_behind: list[dict] = field(default_factory=list)
    #: [{"kind", "detail"}] — what will stop working, or change, after the move.
    stops_working: list[dict] = field(default_factory=list)

    def moves(self, table: str, rows: int, how: str) -> None:
        self.tables.append({"table": table, "rows": rows, "how": how})

    def leaves(self, table: str, rows: int, why: str) -> None:
        if rows:
            self.left_behind.append({"table": table, "rows": rows, "why": why})

    def breaks(self, kind: str, detail: str) -> None:
        self.stops_working.append({"kind": kind, "detail": detail})

    @property
    def counts(self) -> dict[str, int]:
        return {t["table"]: t["rows"] for t in self.tables}

    def as_dict(self, *, dry_run: bool) -> dict:
        return {
            "agent": self.agent.slug,
            "source": self.source.slug,
            "destination": self.dest.slug,
            "dry_run": dry_run,
            "moved": not dry_run,
            "tables": self.tables,
            "left_behind": self.left_behind,
            "stops_working": self.stops_working,
        }


#: (plan, apply) -> None. Counts into `plan`; when `apply`, also writes.
Participant = Callable[[Plan, bool], None]
_PARTICIPANTS: list[Participant] = []


def register(participant: Participant) -> Participant:
    """Add a table-owner's participant. Idempotent (an app's ready() may rerun)."""
    if participant not in _PARTICIPANTS:
        _PARTICIPANTS.append(participant)
    return participant


# ---- the gate ----------------------------------------------------------------

def check(agent, dest_slug: str, *, by) -> Workspace:
    """The destination, after the move gate — or a MoveError naming why not.

    Same gate the upsert's move has always had: the agent's admin where it is
    (moving re-decides every gate that hangs off its tenant, so an editor must
    not be able to carry an agent into a workspace they own), and editor or
    above in the destination. A missing destination and one the caller is not
    in answer the same 404, so this is no oracle over workspace names."""
    dest = Workspace.objects.filter(slug=(dest_slug or "").strip()).first()
    if dest is None or not wsvc.is_member(by, dest.slug):
        raise MoveError(f"workspace '{dest_slug}' not found", status=404)
    if not agent.is_admin(by):
        raise MoveError("moving an agent requires being its owner or one of its admins", status=403)
    if not perms.can(by, dest, perms.AGENT_WORK):
        raise MoveError("moving an agent requires the editor role or above in the destination "
                        "workspace", status=403)
    if dest.slug == agent.workspace_id:
        raise MoveError(f"{agent.slug} is already in '{dest.slug}'", status=422)

    stewarded = stewarded_workspace_projects(agent)
    if stewarded:
        # A workspace project stays in its workspace; its steward must be there too.
        raise MoveError(f"{agent.slug} stewards workspace project(s) {', '.join(stewarded)}, which "
                        f"stay in '{agent.workspace_id}'; hand each to another steward first", status=409)
    return dest


# ---- the operation -----------------------------------------------------------

def plan(agent, dest: Workspace, *, by) -> Plan:
    """The dry run: what `move` would do, computed by the same participants."""
    p = Plan(agent=agent, source=agent.workspace, dest=dest, by=by)
    for participant in _PARTICIPANTS:
        participant(p, False)
    return p


def move(agent, dest: Workspace, *, by) -> Plan:
    """Re-home `agent` and every agent-scoped row in `dest`, atomically.

    Returns the plan it carried out. Raises MoveError (409) if a row cannot be
    re-homed — a unique constraint in the destination — and then nothing moved.
    Writes one `events.Event` in BOTH workspaces naming who moved what."""

    try:
        with transaction.atomic():
            locked = Agent.objects.select_for_update().select_related("workspace").get(pk=agent.pk)
            if locked.workspace_id == dest.slug:
                raise MoveError(f"{agent.slug} is already in '{dest.slug}'", status=422)
            p = Plan(agent=locked, source=locked.workspace, dest=dest, by=by)
            for participant in _PARTICIPANTS:
                participant(p, True)
            locked.workspace = dest
            locked.save(update_fields=["workspace"])
            _record(p)
    except IntegrityError as exc:
        raise MoveError(f"cannot move {agent.slug}: a row would collide with one already in "
                        f"'{dest.slug}' ({exc}); nothing was moved", status=409) from None
    agent.workspace = dest
    agent.workspace_id = dest.slug
    logger.info("agent %s moved %s -> %s by user %s", agent.slug, p.source.slug, dest.slug,
                getattr(by, "pk", None))
    return p


def _record(p: Plan) -> None:
    """One audit event in each workspace: who moved what, with the counts."""

    who = getattr(p.by, "email", "") or "someone"
    payload = {"agent": p.agent.slug, "source": p.source.slug, "destination": p.dest.slug, "by": who,
               "counts": p.counts, "left_behind": p.left_behind,
               "stops_working": p.stops_working}
    for ws, kind, summary in (
        (p.source, "agent.moved_out", f"{who} moved agent {p.agent.slug} from here to '{p.dest.slug}'"),
        (p.dest, "agent.moved_in", f"{who} moved agent {p.agent.slug} here from '{p.source.slug}'"),
    ):
        events.record([{"source": "agents.move", "kind": kind, "level": "info",
                        "summary": summary[:500], "payload": payload}], workspace=ws)


# ---- participants (framework tables) ------------------------------------------
# Each counts its rows into the plan and, when `apply`, writes them. Order is the
# order the dry run lists them in.

def _rehome(qs, dest, apply: bool) -> int:
    """Rows in `qs` not already in `dest`; re-homed when applying."""
    qs = qs.exclude(workspace_id=dest.slug)
    return qs.update(workspace=dest) if apply else qs.count()


@register
def _turns(p: Plan, apply: bool) -> None:

    turns = Turn.objects.filter(agent=p.agent)
    total = turns.count()
    # An agent turn derives its tenant from agent.workspace; a few older rows
    # carry their own FK as well, and those are rewritten so nothing disagrees.
    own = _rehome(turns.filter(workspace__isnull=False), p.dest, apply)
    p.moves("harness.Turn", total, "follows the agent" + (f"; {own} carrying their own "
                                                           "workspace rewritten" if own else ""))
    live = turns.filter(status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN]).count()
    if live:
        p.breaks("turns_in_flight", f"{live} turn(s) are running or waiting on a human now; they "
                                    f"finish, and read as '{p.dest.slug}' from the move on")


@register
def _sessions(p: Plan, apply: bool) -> None:

    sessions = Session.objects.filter(agent=p.agent)
    moved, dropped = filing.rehome_agent_sessions(sessions, p.dest, apply=apply)
    p.moves("canopy_sessions.Session", moved, "re-homed")
    p.moves("canopy_sessions.Message", Message.objects.filter(session__agent=p.agent).count(),
            "follows its session")
    if dropped:
        p.breaks("chat_participants",
                 f"{dropped} chat participant(s) are not members of '{p.dest.slug}' and lose "
                 "access to those chats (a chat is readable only inside its workspace)")


@register
def _capabilities(p: Plan, apply: bool) -> None:

    p.moves("capabilities.CapabilityGrant",
            _rehome(CapabilityGrant.objects.filter(agent=p.agent), p.dest, apply), "re-homed")
    p.moves("capabilities.CapabilityLease",
            _rehome(CapabilityLease.objects.filter(agent=p.agent), p.dest, apply), "re-homed")


@register
def _policy(p: Plan, apply: bool) -> None:

    p.moves("admission.AdmissionRule",
            _rehome(AdmissionRule.objects.filter(agent=p.agent), p.dest, apply),
            "re-homed (rules scoped to this agent)")
    p.moves("retention.RetentionRule",
            _rehome(RetentionRule.objects.filter(agent=p.agent), p.dest, apply),
            "re-homed (rules scoped to this agent)")
    # Workspace-wide rules are the TENANT's, so they do not travel; the agent
    # leaves one set and comes under another, which an owner should see.
    src = AdmissionRule.objects.filter(workspace=p.source, agent__isnull=True).count()
    dst = AdmissionRule.objects.filter(workspace=p.dest, agent__isnull=True).count()
    if src or dst:
        p.breaks("admission_rules",
                 f"{src} workspace-wide admission rule(s) in '{p.source.slug}' stop applying to "
                 f"{p.agent.slug}; {dst} in '{p.dest.slug}' start applying")


@register
def _origin_issues(p: Plan, apply: bool) -> None:

    p.moves("issues.OriginIssue",
            _rehome(OriginIssue.objects.filter(agent=p.agent.slug), p.dest, apply), "re-homed")


@register
def _follows(p: Plan, apply: bool) -> None:
    """Agent-keyed tables with no workspace FK: they move by moving the agent."""

    for model in (AgentTask, AgentProject, AgentSkill, AgentSchedule, RunnerAssignment):
        p.moves(model._meta.label, model.objects.filter(agent=p.agent).count(), "follows the agent")


@register
def _people(p: Plan, apply: bool) -> None:

    p.leaves("contacts.PersonFact",
             PersonFact.objects.filter(asserted_by_agent=p.agent, workspace=p.source).count(),
             "facts about people belong to the workspace that learned them")
    grants = PersonGrant.objects.filter(agent=p.agent, workspace=p.source,
                                        status=PersonGrant.ACTIVE).count()
    p.leaves("contacts.PersonGrant", grants,
             "a person's grant reaches one workspace's entries, by their own act")
    if grants:
        p.breaks("person_grants",
                 f"{grants} active person grant(s) to {p.agent.slug} reach '{p.source.slug}' "
                 f"and stop applying; each person grants again in '{p.dest.slug}'")


@register
def _identity(p: Plan, apply: bool) -> None:
    """Who holds the agent after the move — `Agent.is_admin` is decided in its
    workspace — and the agent's own canopy login."""

    agent, dest = p.agent, p.dest
    if agent.owner_id and not wsvc.is_member(agent.owner, dest.slug):
        p.breaks("owner", f"the agent's owner {agent.owner.email} is not a member of "
                          f"'{dest.slug}' and stops being its admin")
    lost = [a.user.email for a in AgentAdmin.objects.filter(agent=agent).select_related("user")
            if not wsvc.is_member(a.user, dest.slug)]
    if lost:
        p.breaks("admins", f"admin(s) {', '.join(sorted(lost))} are not members of '{dest.slug}' "
                           "and stop being its admins")
    login = agent.user
    if login is not None and not wsvc.is_member(login, dest.slug):
        if perms.can(p.by, dest, perms.MEMBERS_MANAGE):
            # The agent's own login is part of the agent: it joins the new home
            # through the door that made it a member at home (`agent_login`),
            # and only when the mover could add a member there anyway.
            if apply:

                follow_agent(agent, p.by, dest)
            p.moves("workspaces.WorkspaceMembership", 1,
                    f"the agent's own login {login.email} joins '{dest.slug}'")
        else:
            p.breaks("agent_login",
                     f"the agent's own login {login.email} is not a member of '{dest.slug}', "
                     "so its PAT cannot reach the agent there until a workspace admin adds it")


@register
def _runtime(p: Plan, apply: bool) -> None:
    """Runner routing, the shared vault and Slack — re-decided by the new tenant."""

    agent, dest = p.agent, p.dest
    after = copy.copy(agent)
    after.workspace, after.workspace_id = dest, dest.slug
    runners = {a.runner for a in RunnerAssignment.objects.filter(agent=agent).select_related(
        "runner", "runner__owner") if a.runner is not None}
    for r in sorted(runners, key=lambda r: r.name):
        if dest.slug not in routing.runner_tenant_slugs(r):
            p.breaks("runner", f"runner {r.name} does not serve '{dest.slug}' (its owner's "
                               "workspaces it is enrolled in), so it stops taking this agent's turns")
        elif not runner_may_hold_agent(r, after):
            p.breaks("runner", f"runner {r.name}'s owner is not one of {agent.slug}'s admins in "
                               f"'{dest.slug}', so it stops taking this agent's turns")
    if not runners:
        p.breaks("runner_order", f"{agent.slug} has no runner list of its own; it runs on "
                                 f"'{dest.slug}''s default runner order from now on")

    src_vault, dst_vault = p.source.shared_vault_source(), dest.shared_vault_source()
    if (src_vault and src_vault.slug) != (dst_vault and dst_vault.slug):
        p.breaks("shared_vault",
                 f"the shared vault comes from {_vault_name(dst_vault)} instead of "
                 f"{_vault_name(src_vault)}; credentials inherited from it may differ")

    if agent.slack_enabled:

        before, now = installation_for_workspace(p.source.slug), installation_for_workspace(dest.slug)
        if (before and before.pk) != (now and now.pk):
            p.breaks("slack", f"'{dest.slug}' is " + (f"connected to a different Slack ({now.team_name})"
                                                      if now else "not connected to Slack")
                     + f", so {agent.slug}'s Slack command stops working where it was")


def _vault_name(ws) -> str:
    return f"'{ws.slug}'" if ws is not None else "no workspace (the box's built-in default)"
