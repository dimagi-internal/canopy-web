"""Which MODE a turn runs in — the other half of a routing rule.

`manual` / `auto`, the ladder below, and who may ask for which are defined in
`docs/architecture/access.md`; the person-side half (who may request `auto`,
whose turns are always manual) is `apps/agents/access.decide`.

A routing rule (`RunnerAssignment` with a non-empty `source`) already answers
"which box runs this person's work on this channel". Spec 2026-09-23 lets the
same rule answer "and may the agent act on it without asking first":
`eva / email / beth@… → cloud, auto`.

The ladder is the routing ladder, read for its `turn_mode` instead of its
runners: the (source, actor) rule, then the (source, anyone) rule, then
`Agent.turn_mode`. A rung whose mode is "" says nothing and the next one decides,
so a rule that only moves work to another box leaves the agent's posture alone.

**An `auto` that names a person needs a verified message.** Routing on a forged
`From:` is harmless — it only chooses which machine runs the turn. Auto on one
would hand whoever forged Beth's address an agent that sends without review. So
an actor rule's `auto` applies only when `caller_context._verified(turn)` holds
for THIS message; otherwise the turn runs MANUAL and the basis says why. Not
"fall through to the agent's mode": the rule was written about Beth, and a
message that may not be Beth is exactly the case to slow down on. A `manual`
rule is always honoured — lowering autonomy is safe from anyone.

An anyone-rule (`actor=""`) makes no claim about who sent it, so it needs no
verification: `email → auto` means what the agent-wide switch already means.

**A dispatch may ask for a mode, and that request is the top rung.** An admin
who sends an agent work names the posture for THAT turn (`TurnIn.turn_mode`,
stamped as `Turn.requested_turn_mode`), above every rule and the agent's own
switch — the rules are standing policy about a channel, the request is a
decision about this one piece of work. Same philosophy as the rules: `manual`
is honoured from anyone who may enqueue; `auto` only from the agent's owner or
an admin (`Agent.is_admin`, which counts workspace owners), and that is checked
twice — at enqueue (403) and again here, at every claim, so an admin revoked
while the turn sat queued gets manual, with the basis saying why.

**A workspace editor's turns are always manual** (owner decision, 2026-10-04).
An editor who is not an agent admin may reshape the agent and send it work, so
their turn runs in its full profile — but never in `auto`: whatever a rule or the
agent's switch says, the turn is manual and its basis says why, because acting
outbound unreviewed is for the agent's owner and admins. Decided at every claim
(`access.decide_for_turn`), so promoting the editor to admin while the turn is
queued takes effect, as does a demotion.

Pure given the loaded rows, like `services.assignment_rows_for` — callable from
the claim path (which has them) and from the envelope (which loads them).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import actors

MANUAL, AUTO = "manual", "auto"
MODES = (MANUAL, AUTO)


@dataclass(frozen=True)
class Resolved:
    mode: str
    # Human-readable, repeated by the agent in its turn opening.
    basis: str


def _rule_label(origin: str, actor: str) -> str:
    return f"rule {origin}/{actor or 'anyone'}"


def resolve(*, agent, origin: str, actor: str, verified: bool, priorities: dict) -> Resolved:
    """The mode for one (agent, origin, actor), given `load_assignment_rows`' rules."""
    exact = priorities.get((agent.id, origin, actor)) if actor else None
    anyone = priorities.get((agent.id, origin, ""))
    for rule, named in ((exact, True), (anyone, False)):
        if not rule:
            continue
        mode = rule[0].turn_mode
        if mode not in MODES:
            continue
        label = _rule_label(origin, actor if named else "")
        if mode == AUTO and named and not verified:
            return Resolved(MANUAL, f"{label}: auto withheld, message not verified")
        return Resolved(mode, label)
    mode = agent.turn_mode if agent.turn_mode in MODES else MANUAL
    return Resolved(mode, "agent")


def requested(turn, agent) -> Resolved | None:
    """The dispatcher's requested mode for an agent turn, or None if it asked for none."""
    mode = getattr(turn, "requested_turn_mode", "") or ""
    if mode not in MODES or agent is None or not turn.agent_id:
        return None
    user = turn.requested_turn_mode_by if turn.requested_turn_mode_by_id else None
    if user is None:
        who_ = "a deleted user"
        if mode == AUTO:
            return Resolved(MANUAL, f"dispatch by {who_}: auto withheld, requester gone")
        return Resolved(MANUAL, f"dispatch by {who_}")
    from .caller_context import relationship_for_user

    label = f"dispatch by {user.email} ({relationship_for_user(user, agent)})"
    if mode == AUTO and not agent.is_admin(user):
        return Resolved(MANUAL, f"{label}: auto withheld, not an admin of {agent.slug}")
    return Resolved(mode, label)


def editor_cap(turn, agent) -> Resolved | None:
    """MANUAL for a turn started by a workspace editor who is not an agent admin
    (the editor tier of docs/architecture/access.md), else None."""
    if getattr(turn, "initiator_user_id", None) is None:
        return None
    from apps.agents import access

    if not access.decide_for_turn(turn, agent).manual_only:
        return None
    email = turn.initiator_user.email if turn.initiator_user is not None else "a member"
    return Resolved(MANUAL, f"editor {email}: manual — outbound needs an admin of {agent.slug}")


def _agent_of(turn):
    if turn.agent_id:
        return turn.agent
    cs = getattr(turn, "chat_session", None)
    return cs.agent if cs is not None and cs.agent_id else None


def for_turn(turn, priorities: dict | None = None, *, fresh: bool = False) -> Resolved | None:
    """The mode for a turn, or None for one with no agent (a project turn).

    Reads the claim-time stamp when there is one, so a rule edited mid-turn does
    not change the posture of work already under way. Otherwise resolves live —
    loading the rules when the caller has not (an envelope read of a queued turn).
    `fresh=True` is the claim path: a turn whose lease expired comes back QUEUED
    still carrying its last stamp, and the new claim must decide again.
    """
    if turn.turn_mode and not fresh:
        return Resolved(turn.turn_mode, turn.turn_mode_basis or "")
    agent = _agent_of(turn)
    if agent is None:
        return None
    asked = requested(turn, agent)
    if asked is not None:
        return asked
    capped = editor_cap(turn, agent)
    if capped is not None:
        return capped
    if priorities is None:
        from .services import load_assignment_rows

        _defaults, priorities = load_assignment_rows([agent.id])
    from .caller_context import _verified

    return resolve(
        agent=agent, origin=turn.origin, actor=actors.actor_of(turn),
        verified=_verified(turn), priorities=priorities,
    )
