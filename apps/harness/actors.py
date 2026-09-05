"""Who a turn is FROM — the actor half of the routing key.

Source rules (spec 2026-07-27) answer "which box runs this KIND of work".
Actor rules (spec 2026-09-05) answer "which box runs THIS PERSON'S work", which
is what lets a cloud runner take other people's work while the operator's own
work stays on the operator's boxes. This module is the resolution half: one pure
function mapping a turn to a normalized actor.

The actor is DERIVED, never stored. A second column beside `origin_ref` and
`enqueued_by` would be a third writer of the same fact, which is exactly the
mistake the source spec avoided when it declined to add `source` beside `origin`.

Two things that are NOT true, and which the obvious implementation gets wrong:

  1. `Turn.enqueued_by` on an EMAIL turn is not the sender. It is set once, in
     `harness/api.py` as `request.user` — the account the runner's inbox watcher
     authenticates as. Every email turn in production reads the runner owner's
     address regardless of who wrote in, so keying on it collapses every
     correspondent onto one rule. The sender is in `origin_ref["from"]`, written
     by `runner/canopy_runner/canopy_runner/inbox.py`.
  2. Chat and ace-web turns carry no `origin_ref["from"]` at all. Their actor is
     `enqueued_by` — which the session-send path only began setting alongside
     this spec.

So there is no single field. There is a per-origin rule, and it is this table.
"""
from __future__ import annotations

from email.utils import parseaddr

from apps.harness.models import Turn

#: Origins whose actor is the SENDER, read out of `origin_ref["from"]`.
_SENDER_ORIGINS = frozenset({Turn.ORIGIN_EMAIL})

#: Origins with no human actor at all. A schedule fires on a clock; matching it
#: against a person's rule would route it by accident, so it resolves to "" and
#: falls through to the source rule. (Extension point, if this is ever wanted:
#: `AgentSchedule.created_by`.)
_ACTORLESS_ORIGINS = frozenset({Turn.ORIGIN_CANOPY_SCHEDULER})


def normalize_actor(value: str | None) -> str:
    """A bare, lowercased address — or "" when there isn't one.

    Accepts both shapes a rule or a header can carry: `addr@host` and
    `Display Name <addr@host>`. Uses `parseaddr` rather than slicing on `<`,
    because a quoted display name may contain the delimiters a hand-rolled parse
    would trip on — `'"Anthropic, PBC" <invoice+statements@mail.anthropic.com>'`
    is a real value in the fleet.

    Returns "" for anything that does not resolve to something address-shaped.
    Empty is the safe answer: it matches no actor rule, so the turn falls through
    to the source rule and then the default order. A partial or guessed value
    would silently route instead.
    """
    _, addr = parseaddr((value or "").strip())
    addr = addr.strip().lower()
    # parseaddr is lenient — it returns the input unchanged for plenty of
    # non-addresses. Require the one structural thing an address must have.
    if "@" not in addr:
        return ""
    local, _, domain = addr.partition("@")
    return addr if local and domain else ""


def resolve_actor(origin: str, origin_ref, enqueued_by_email: str | None) -> str:
    """The normalized actor for one turn, or "" when it has none.

    Pure: no queries, no clock. Callable from `claim_next_turn`,
    `unclaimable_queued_turns` and the rules API alike, which is what keeps the
    three from disagreeing about who a turn is from.
    """
    if origin in _ACTORLESS_ORIGINS:
        return ""
    if origin in _SENDER_ORIGINS:
        # `origin_ref` is a JSONField and a malformed row must not break claiming
        # for the whole fleet, so this tolerates a non-dict rather than trusting
        # the shape.
        ref = origin_ref if isinstance(origin_ref, dict) else {}
        return normalize_actor(ref.get("from"))
    return normalize_actor(enqueued_by_email)


def actor_of(turn: Turn) -> str:
    """`resolve_actor` for a loaded Turn. The one place that knows an email is
    reached through the `enqueued_by` FK, so callers can select_related it once
    rather than each re-deriving the traversal."""
    email = turn.enqueued_by.email if turn.enqueued_by_id else ""
    return resolve_actor(turn.origin, turn.origin_ref, email)
