"""THE way anything records an analytics fact: `emit`.

One writer for `Activity`, so every producer stamps the same who / where / what /
when and analytics is one indexed read (`apps/events/analytics.py`). A producer
never builds an `Activity` row itself (`tests/test_activity_has_one_writer.py`).

Recording a fact must never be able to undo the action it describes: `emit` runs
in its own savepoint and swallows its own failure (logged), so a bad fact cannot
poison the caller's transaction. Turn facts go through `for_turn`, which derives
tenancy, agent and person from the turn exactly once.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .models import Activity

logger = logging.getLogger(__name__)

#: The vocabulary. A new kind is added here first, so a reader of the table can
#: see every kind that exists without grepping producers.
TURN_ENQUEUED = "turn.enqueued"
TURN_FINISHED = "turn.finished"
CHAT_SESSION_CREATED = "chat.session.created"
CHAT_MESSAGE = "chat.message"
SETTING_CHANGED = "setting.changed"
MCP_CALL = "mcp.call"
#: A new cloud box's pairing invitation: made, approved, and used to pair
#: (`apps/harness/api_runner_invitations.py`).
RUNNER_INVITED = "runner.invited"
RUNNER_INVITATION_APPROVED = "runner.invitation.approved"
RUNNER_PAIRED = "runner.paired"
#: A laptop box's browser pairing approved by a person
#: (`apps/harness/api_runner_pairings.py`); `runner.paired` follows when it claims.
RUNNER_PAIRING_APPROVED = "runner.pairing.approved"
#: A cloud runner's device key was replaced by its box re-pairing (a replaced
#: instance, or a box adopted into isolation units).
RUNNER_REKEYED = "runner.rekeyed"
#: canopy-web changed a runner fleet's size (`apps/harness/fleets.py`): `from` and
#: `to` are the group's desired capacity, so the series is how many members ran.
RUNNER_FLEET_SCALED = "runner.fleet.scaled"
KINDS = (TURN_ENQUEUED, TURN_FINISHED, CHAT_SESSION_CREATED, CHAT_MESSAGE, SETTING_CHANGED, MCP_CALL,
         RUNNER_INVITED, RUNNER_INVITATION_APPROVED, RUNNER_PAIRED, RUNNER_PAIRING_APPROVED, RUNNER_REKEYED,
         RUNNER_FLEET_SCALED)


def _actor_kind(actor, agent=None) -> str:
    """Who `actor` is, from the one record of each: an agent's own canopy login
    (`Agent.user`) is the agent — Ada sending Hal work is not a person, whichever
    agent the fact is about — and a system account (`system_accounts`, e.g. a
    CloudWatch sender) is the system. Everyone else is a person."""
    from django.core.exceptions import ObjectDoesNotExist

    from apps.workspaces.system_accounts import is_system_user

    if actor is None:
        return Activity.SYSTEM
    if agent is not None and agent.is_own_login(actor):
        return Activity.AGENT
    try:
        if actor.agent_identity is not None:
            return Activity.AGENT
    except ObjectDoesNotExist:
        pass
    return Activity.SYSTEM if is_system_user(actor) else Activity.USER


def reclassify() -> int:
    """Re-apply `_actor_kind` to facts already recorded as a person — for facts
    written before an actor's login was known to be an agent's or a system's.
    Returns how many changed."""
    people = Activity.objects.filter(actor_kind=Activity.USER)
    n = people.filter(actor__agent_identity__isnull=False).update(actor_kind=Activity.AGENT)
    return n + people.filter(actor__system_account__isnull=False).update(actor_kind=Activity.SYSTEM)


def repo_of(*, turn=None, session=None) -> str:
    """The repository a fact is about when it is not an agent's work: a repo turn's
    `project`, or the `project` of a session no agent runs (a person's own Claude
    Code session in a repo). Blank for agent work — an agent's chat keeps its
    worktree in `Session.emdash_project`, and its agent is the dimension. THE one
    rule, for facts and token usage alike."""
    if turn is not None:
        if turn.agent_id:
            return ""
        if turn.chat_session_id:
            session = turn.chat_session
        else:
            return turn.project or ""
    if session is not None and not session.agent_id:
        return session.project or ""
    return ""


def emit(kind: str, *, workspace, actor=None, actor_kind: str = "", agent=None,
         surface: str = "", ref: str = "", meta: dict | None = None, at=None,
         dedupe: bool = False, project: str = "") -> Activity | None:
    """Record one fact. `workspace` is a Workspace or its slug and is required —
    a fact with no tenant is dropped (logged), never stored as visible-to-all.

    `dedupe=True` skips the write when a fact of this kind already names `ref`;
    the backfill uses it so a re-run never double-counts."""
    if kind not in KINDS:
        raise ValueError(f"unknown activity kind {kind!r}; add it to activity.KINDS")
    slug = getattr(workspace, "slug", workspace)
    if not slug:
        logger.warning("activity %s dropped: no workspace (ref=%s)", kind, ref)
        return None
    actor = actor if getattr(actor, "is_authenticated", False) else None
    try:
        with transaction.atomic():
            if dedupe and ref and Activity.objects.filter(kind=kind, ref=ref).exists():
                return None
            return Activity.objects.create(
                workspace_id=slug, kind=kind, at=at or timezone.now(),
                actor=actor, actor_kind=actor_kind or _actor_kind(actor, agent),
                agent=agent, agent_slug=agent.slug if agent is not None else "",
                project=project[:100], surface=surface[:32], ref=str(ref)[:64], meta=meta or {},
            )
    except Exception:  # noqa: BLE001 — a fact is never worth a failed action
        logger.exception("could not record activity %s (ref=%s)", kind, ref)
        return None


#: Turn origin -> the surface it arrived through, in the words the dashboard shows.
_ORIGIN_SURFACE = {
    "canopy_web_chat": "chat", "canopy_scheduler": "scheduler", "email": "email",
    "slack": "slack", "ace_web": "ace-web", "api": "api",
}


def turn_actor(turn):
    """The person a turn is for: who asked (`initiator_user`), else who enqueued it."""
    return turn.initiator_user if turn.initiator_user_id else (
        turn.enqueued_by if turn.enqueued_by_id else None)


def for_turn(kind: str, turn, *, at=None, dedupe: bool = False, **meta) -> Activity | None:
    """A turn fact: tenancy from `Turn.tenant_slug`, agent from `acting_agent`,
    person from `turn_actor`, surface from the origin, and — once a box has it —
    the runner it ran on (`runner`, `runner_kind`), kept by name so the fact
    still says where after the box is retired."""
    agent = turn.acting_agent
    actor = turn_actor(turn)
    kind_of_actor = (Activity.CONTACT if turn.initiator_contact_id and actor is None
                     else Activity.AGENT if turn.initiator_agent and actor is None
                     else _actor_kind(actor, agent))
    return emit(kind, workspace=turn.tenant_slug, actor=actor, actor_kind=kind_of_actor,
                agent=agent, surface=_ORIGIN_SURFACE.get(turn.origin, turn.origin),
                ref=f"{turn.pk}", at=at, dedupe=dedupe, project=repo_of(turn=turn),
                meta={"status": turn.status, "origin": turn.origin, **runner_of(turn),
                      **who_and_where(turn, actor if kind_of_actor == Activity.AGENT else None), **meta})


def thread_of(turn) -> str:
    """The conversation a turn belongs to, as `session:<uuid>` (a chat, which is
    also where a bound Slack thread lives) or `mail:<uuid>` (an email thread), or
    "" for a turn that is not part of one (a schedule, a bare API dispatch)."""
    if turn.chat_session_id:
        return f"session:{turn.chat_session_id}"
    thread_id = (turn.origin_ref or {}).get("thread_id") if isinstance(turn.origin_ref, dict) else None
    if turn.origin == "email" and thread_id:
        return f"mail:{thread_id}"
    return ""


def who_and_where(turn, agent_login=None) -> dict:
    """The ids the network view (`analytics.network`) joins on, none of them
    content: the thread, the contact who asked, and the agent that asked —
    `initiator_agent`, else the agent whose own login the actor is."""
    out = {}
    if thread := thread_of(turn):
        out["thread"] = thread
    if turn.initiator_contact_id:
        out["contact"] = turn.initiator_contact_id
    by_agent = turn.initiator_agent
    if not by_agent and agent_login is not None:
        identity = getattr(agent_login, "agent_identity", None)
        by_agent = identity.slug if identity is not None else ""
    if by_agent:
        out["by_agent"] = by_agent
    return out


def runner_of(turn) -> dict:
    """{runner, runner_kind} of the box that claimed `turn`, or {} when none has."""
    box = turn.claimed_by if turn.claimed_by_id else None
    return {"runner": box.name, "runner_kind": box.kind} if box is not None else {}
