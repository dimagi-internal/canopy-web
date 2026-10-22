"""Alert source `unclaimable_turns`: queued turns no runner will ever take.

`claim.unclaimable_queued_turns` finds queued turns that look stuck and says
why. Two kinds matter differently: `config` (no runner can target the turn at
all — agent unrouted, repo undeclared, a pin to a retired box…) never runs until
someone edits routing, so it is `blocking`; `offline` (a runner could take it
but none is reachable) is usually transient and raises nothing — the bar is
configuration, not liveness. Held and parked turns are someone's decision and
are not reported either.

One alert per agent (`agent:<slug>:unclaimable`), its fingerprint the sorted
distinct `reason` strings of its stuck turns (hashed: `Alert.fingerprint` is 64
characters) — the CAUSE, not the turn ids. Another turn stuck for the same reason,
or one of them cancelled, is a repeat, not a new fact: a dismissed alert stays
dismissed (and nobody is mailed again); a new cause reopens it. The title's count
still follows the turns (a title refresh logs no event). It resolves when none
of the agent's turns are config-stuck any more.

Actions: `cancel` cancels each stuck turn through `services.cancel_queued_turn`,
offered only while turns are stuck and the viewer may cancel EVERY one of them
(the same `_cancel_refusal` the cancel route and the unclaimable list use).
`route` (a link to the agent's Settings tab, where routing lives) and the shared
dismiss go to the same people plus whoever may edit the agent's routing
(`agent.work`, as `agent_cant_run` uses) — the latter even when nothing is stuck
any more, so a resolved alert's history stays readable to them. Turns not
addressed to an agent (a project turn) have no agent to group under and are
left to the supervisor's list.

Cost: the diagnosis (`claim.unclaimable_queued_turns`, fleet-wide) is heavy, and
`actions()` runs per viewer per alert on every list read, act and notify. So
`stuck_by_agent()` is memoised per process for `MEMO_TTL_S`: the tick's
reconcile refreshes it, `actions()` reads it, and `perform` — the safety
re-check before cancelling — always reads fresh.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from apps.agents.models import Agent
from apps.attention.alerts import Action, Raise
from apps.workspaces import permissions as perms
from apps.workspaces.models import Workspace

from . import claim, services
from .api_turns import _cancel_refusal
from .models import Turn

KIND = "unclaimable_turns"
SEVERITY = "blocking"
CONFIRM_WITHIN = timedelta(minutes=30)
CLOSING_ACTION_IDS = ("cancel", "route")


def settings_path(agent) -> str:
    """Where an agent's routing is edited (its Settings tab). App-relative."""
    return f"/w/{agent.workspace_id}/agents/{agent.slug}/settings"


def turns_path(agent) -> str:
    return f"/w/{agent.workspace_id}/agents/{agent.slug}/turns"


#: How long `actions()` may reuse a diagnosis (seconds). Short: the tick
#: refreshes it every minute, and `perform` never trusts it.
MEMO_TTL_S = 30


@dataclass
class Stuck:
    """One agent's config-stuck turns, and why (claim's `reason` strings)."""

    turns: list[Turn] = field(default_factory=list)
    reasons: set[str] = field(default_factory=set)


_memo: dict = {"at": 0.0, "value": None}


def forget() -> None:
    """Drop the memoised diagnosis (tests; after a cancel)."""
    _memo.update(at=0.0, value=None)


def _diagnose() -> dict[str, Stuck]:
    rows = [r for r in claim.unclaimable_queued_turns(ws_slugs=set(Workspace.objects.values_list("slug", flat=True)))
            if r["kind"] == "config"]
    turns = Turn.objects.select_related("agent", "chat_session", "chat_session__agent").in_bulk(
        [uuid.UUID(r["turn_id"]) for r in rows])
    out: dict[str, Stuck] = defaultdict(Stuck)
    for r in rows:
        turn = turns.get(uuid.UUID(r["turn_id"]))
        agent = turn.acting_agent if turn is not None else None
        if agent is not None:
            out[agent.slug].turns.append(turn)
            out[agent.slug].reasons.add(str(r["reason"]))
    return dict(out)


def stuck_by_agent(*, fresh: bool = False) -> dict[str, Stuck]:
    """The config-stuck queued turns, by the slug of the agent they act as.
    Memoised for `MEMO_TTL_S` unless `fresh` (which also refreshes the memo)."""
    now = time.monotonic()
    if not fresh and _memo["value"] is not None and now - _memo["at"] < MEMO_TTL_S:
        return _memo["value"]
    value = _diagnose()
    _memo.update(at=now, value=value)
    return value


def fingerprint(stuck: Stuck) -> str:
    """The cause: the sorted distinct reasons, hashed to fit 64 characters."""
    return hashlib.sha256("\n".join(sorted(stuck.reasons)).encode()).hexdigest()


def _title(agent, n: int) -> str:
    if n == 1:
        return f"1 turn for {agent.name} is stuck: no runner can take it"
    return f"{n} turns for {agent.name} are stuck: no runner can take them"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    stuck = stuck_by_agent(fresh=True)
    agents = {a.slug: a for a in Agent.objects.filter(slug__in=stuck)}
    for slug, found in sorted(stuck.items()):
        agent = agents.get(slug)
        if agent is None:
            continue
        out.append(Raise(
            subject_key=f"agent:{slug}:unclaimable",
            workspace=agent.workspace_id,
            title=_title(agent, len(found.turns)),
            url=turns_path(agent),
            scopes=(f"agent:{slug}", f"workspace:{agent.workspace_id}"),
            fingerprint=fingerprint(found),
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
    turns = stuck_by_agent().get(agent.slug, Stuck()).turns
    return bool(turns) and all(not _cancel_refusal(user, t) for t in turns)


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    agent = _agent_of(alert)
    if agent is None:
        return []
    cancel = _cancellable_stuck(agent, user)
    if not cancel and not perms.can(user, agent.workspace_id, perms.AGENT_WORK):
        return []
    return [Action("route", "Fix routing", True, href=settings_path(agent))] + (
        [Action("cancel", "Cancel turns", True)] if cancel else [])


def perform(alert, user, action_id) -> None:
    if action_id != "cancel":
        raise ValueError(f"unclaimable_turns has no handler for {action_id!r}")
    agent = _agent_of(alert)
    if agent is None:
        return
    # Re-check on a FRESH read before touching anything: a partial cancel must
    # not resolve, and the memo may be up to MEMO_TTL_S old.
    turns = stuck_by_agent(fresh=True).get(agent.slug, Stuck()).turns
    if any(_cancel_refusal(user, t) for t in turns):
        raise ValueError("some of these turns may not be cancelled by you; nothing was cancelled")
    for turn in turns:
        services.cancel_queued_turn(turn)
    forget()  # the memo still lists what was just cancelled
