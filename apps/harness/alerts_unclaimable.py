"""Alert source `unclaimable_turns`: queued turns no runner will ever take.

`claim.unclaimable_queued_turns` finds queued turns that look stuck and says
why. Two kinds matter differently: `config` (no runner can target the turn at
all — agent unrouted, repo undeclared, a pin to a retired box…) never runs until
someone edits routing, so it is `blocking`; `offline` (a runner could take it
but none is reachable) is usually transient and raises nothing — the bar is
configuration, not liveness. Held and parked turns are someone's decision and
are not reported either.

One alert per agent (`agent:<slug>:unclaimable`), its fingerprint the sorted turn
ids (hashed: `Alert.fingerprint` is 64 characters), so a newly stuck turn is a
change on the open alert. It resolves when none of the agent's turns are
config-stuck any more.

Offered to people who may cancel the stuck turns (the same `_cancel_refusal` the
cancel route and the unclaimable list use): `cancel` cancels each of them through
`services.cancel_queued_turn`, `route` is a link to the agent's settings, where
routing lives. Turns not addressed to an agent (a project turn) have no agent to
group under and are left to the supervisor's list.
"""
from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from datetime import timedelta

from apps.agents.models import Agent
from apps.attention.alerts import DISMISS, Action, Raise
from apps.workspaces.models import Workspace

from . import claim, services
from .api_turns import _cancel_refusal
from .models import Turn

KIND = "unclaimable_turns"
SEVERITY = "blocking"
CONFIRM_WITHIN = timedelta(minutes=30)
CLOSING_ACTION_IDS = ("cancel", "dismiss")


def settings_path(agent) -> str:
    """Where an agent's routing is edited (its Settings tab). App-relative."""
    return f"/w/{agent.workspace_id}/agents/{agent.slug}/settings"


def turns_path(agent) -> str:
    return f"/w/{agent.workspace_id}/agents/{agent.slug}/turns"


def stuck_by_agent() -> dict[str, list[Turn]]:
    """The config-stuck queued turns, by the slug of the agent they act as."""
    rows = [r for r in claim.unclaimable_queued_turns(ws_slugs=set(Workspace.objects.values_list("slug", flat=True)))
            if r["kind"] == "config"]
    turns = Turn.objects.select_related("agent", "chat_session", "chat_session__agent").in_bulk(
        [uuid.UUID(r["turn_id"]) for r in rows])
    out: dict[str, list[Turn]] = defaultdict(list)
    for r in rows:
        turn = turns.get(uuid.UUID(r["turn_id"]))
        agent = turn.acting_agent if turn is not None else None
        if agent is not None:
            out[agent.slug].append(turn)
    return out


def _title(agent, n: int) -> str:
    if n == 1:
        return f"1 queued turn for {agent.name} has no runner that can take it"
    return f"{n} queued turns for {agent.name} have no runner that can take them"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    stuck = stuck_by_agent()
    agents = {a.slug: a for a in Agent.objects.filter(slug__in=stuck)}
    for slug, turns in sorted(stuck.items()):
        agent = agents.get(slug)
        if agent is None:
            continue
        ids = sorted(str(t.pk) for t in turns)
        out.append(Raise(
            subject_key=f"agent:{slug}:unclaimable",
            workspace=agent.workspace_id,
            title=_title(agent, len(ids)),
            url=turns_path(agent),
            scopes=(f"agent:{slug}", f"workspace:{agent.workspace_id}"),
            fingerprint=hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        ))
    return out


def _agent_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 3 or parts[0] != "agent" or parts[2] != "unclaimable":
        return None
    return Agent.objects.filter(slug=parts[1], workspace_id=alert.workspace_id).first()


def _cancellable_stuck(agent, user) -> bool:
    """May `user` cancel EVERY config-stuck turn this alert is about? One they
    may not would stay queued after `cancel`, and the framework would resolve
    the alert anyway — so it is offered only when it truly clears the alert."""
    turns = stuck_by_agent().get(agent.slug, [])
    return bool(turns) and all(not _cancel_refusal(user, t) for t in turns)


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    agent = _agent_of(alert)
    if agent is None or not _cancellable_stuck(agent, user):
        return []
    return [Action("cancel", "Cancel these turns", True),
            Action("route", "Fix routing", False, href=settings_path(agent)),
            DISMISS]


def perform(alert, user, action_id) -> None:
    if action_id != "cancel":
        raise ValueError(f"unclaimable_turns has no handler for {action_id!r}")
    agent = _agent_of(alert)
    if agent is None:
        return
    turns = stuck_by_agent().get(agent.slug, [])
    # Re-check before touching anything: a partial cancel must not resolve.
    if any(_cancel_refusal(user, t) for t in turns):
        raise ValueError("some of these turns may not be cancelled by you; nothing was cancelled")
    for turn in turns:
        services.cancel_queued_turn(turn)
