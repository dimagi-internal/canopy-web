"""The decision->work edge: an approved Item becomes Turns.

`dispatch[]` was never a new concept — it is a deferred Turn enqueue. Ada's
`{target_agent, prompt, origin, origin_ref}`, the phone composer's
`{agent_slug, prompt, origin}`, and Turn are three spellings of one payload.
TurnSpec is that payload, named once.

See docs/superpowers/specs/2026-07-15-item-and-turn-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.agents.models import Agent

from . import services
from .models import Item, Turn


@dataclass(frozen=True)
class TurnSpec:
    """One deferred Turn enqueue.

    `target_agent=""` means SELF — the Item's own agent. Self-dispatch is the
    default and needs no ceremony; Ada's cross-agent fan-out is this same field
    set to another slug. A parameter, not a code path.
    """

    prompt: str
    target_agent: str = ""
    origin: str = Turn.ORIGIN_API
    origin_ref: dict = field(default_factory=dict)
    routing: str = Turn.PREFER_LOCAL

    @classmethod
    def from_dict(cls, d: dict) -> TurnSpec:
        return cls(
            prompt=(d.get("prompt") or "").strip(),
            target_agent=(d.get("target_agent") or "").strip(),
            origin=(d.get("origin") or Turn.ORIGIN_API).strip(),
            origin_ref=d.get("origin_ref") or {},
            routing=(d.get("routing") or Turn.PREFER_LOCAL).strip(),
        )


def _with_reply(prompt: str, item: Item) -> str:
    """Carry the human's own words to the agent that will act on them.

    A card's `prompt` is written BEFORE the human replies, so on its own it is the
    proposal, not the decision. The reply is where the steer lives — "yes, but send
    it to hal instead", "only the retry, skip the backoff" — and an agent that never
    sees it executes a brief the human already amended.
    """
    reply = (item.comment or "").strip()
    if not reply:
        return prompt
    return (
        f"{prompt}\n\n---\n"
        f"ANSWERED BY {item.decided_by or 'a human'}: {reply}\n\n"
        f"That reply is the authority on this card and OVERRIDES the brief above wherever "
        f"the two disagree. If it redirects the work, narrows it, declines it, or asks a "
        f"question back, do THAT — and report on the item instead of executing the "
        f"original proposal."
    )


def dispatch(item: Item, *, actor_workspace_slugs: set[str]) -> list[Turn]:
    """Enqueue an approved Item's work. Idempotent per (item, index).

    `actor_workspace_slugs` is the deciding human's workspace memberships. A
    cross-agent dispatch (`target_agent` set) is authorized ONLY if the target's
    workspace is one of them — the hard tenant boundary. This preserves the fleet
    manager (Ada dispatching hal→eva across workspaces works when the human driving
    it is a member of both), while blocking a single-workspace user from landing a
    prompt on another tenant's agent.

    Raises ValueError for an unknown OR cross-tenant target_agent rather than
    skipping it: an approved item whose work silently never happens is the worst
    outcome here. The caller (services.decide_item) runs this inside the same
    transaction as the decision, so a raise rolls the decision back and leaves the
    item OPEN and retryable — rather than stranding it decided-but-undispatched,
    which deciding once (409) would make permanent.
    """
    turns: list[Turn] = []
    for i, raw in enumerate(item.dispatch or []):
        spec = TurnSpec.from_dict(raw)
        if spec.target_agent:
            target = Agent.objects.filter(slug=spec.target_agent).first()
            if target is None:
                raise ValueError(
                    f"item {item.id} dispatch[{i}]: unknown target_agent {spec.target_agent!r}"
                )
            # Cross-agent dispatch is a cross-tenant action unless the actor is a
            # member of the target's workspace. Self-dispatch (below) is already
            # authorized — the actor could decide the item, which required its
            # agent's workspace. Legacy null-workspace targets fall through.
            if target.workspace_id is not None and target.workspace_id not in actor_workspace_slugs:
                raise ValueError(
                    f"item {item.id} dispatch[{i}]: not a member of target_agent "
                    f"{spec.target_agent!r}'s workspace"
                )
        else:
            target = item.agent
        turn, _created = services.enqueue_turn(
            agent=target,
            origin=spec.origin,
            idempotency_key=f"item-{item.id}-{i}",
            prompt=_with_reply(spec.prompt or f"/{target.slug}:turn", item),
            origin_ref=spec.origin_ref,
            routing=spec.routing,
        )
        if turn.raised_from_id is None:
            turn.raised_from = item
            turn.save(update_fields=["raised_from"])
        turns.append(turn)
    return turns
