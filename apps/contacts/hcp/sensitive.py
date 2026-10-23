"""Sensitive conversations: nothing from one is ever written to what canopy knows
about people."""
from __future__ import annotations

import uuid

# --- nothing from a sensitive conversation is ever written ------------------------------
#
# Owner rule (Jonathan, 2026-10-08): nothing should ever be written here from a session on a
# box set up for sensitive data. A sensitive conversation exists so that what is said in it is
# not kept; an entry, or an agent's inference from it, would keep it here.
#
# A turn is sensitive when its conversation is (`harness.sensitivity.is_sensitive`: the host's
# signed requirement, fail-closed on a malformed one) or it was CLAIMED by a runner whose owner
# declared `sensitive_data` (`RunnerFlag`) — and so is any turn descended from one
# (`parent_turn`), since a dispatch carries the session's words onward. An AGENT write that
# names no turn at all is refused, because it cannot be shown not to come from one.

_ANCESTRY_MAX = 20


class SensitiveRefused(Exception):
    """A write from a sensitive conversation. Safe to show the caller."""


def _turn_is_sensitive(turn) -> bool:
    from apps.harness import sensitivity
    from apps.harness.models import RunnerFlag

    if sensitivity.is_sensitive(turn):
        return True
    return bool(turn.claimed_by_id) and RunnerFlag.objects.filter(
        runner_id=turn.claimed_by_id, flag=sensitivity.SENSITIVE_DATA).exists()


def is_sensitive_turn(turn) -> bool:
    """Is this turn — or any turn it descends from — a sensitive conversation's?"""
    seen = set()
    while turn is not None and turn.pk not in seen and len(seen) < _ANCESTRY_MAX:
        seen.add(turn.pk)
        if _turn_is_sensitive(turn):
            return True
        turn = turn.parent_turn if turn.parent_turn_id else None
    return False


def _context_turn():
    """The turn the request in flight was made from (`X-Canopy-Parent-Turn`, which the
    canopy CLI fills from CANOPY_TURN_ID, or a caller token's own turn)."""
    from apps.common import request_context
    from apps.harness.models import Turn

    tid = ((request_context.current().get("parent") or {}).get("turn") or "").strip()
    if not tid:
        return None
    try:
        return Turn.objects.select_related("chat_session").filter(pk=uuid.UUID(tid)).first()
    except ValueError:
        return None


def refuse_if_sensitive(*, source_turn=None, by_agent=None) -> None:
    """Raise SensitiveRefused when this write comes from a sensitive conversation — or
    is an agent's write that cannot say which session it comes from."""
    turns = [t for t in (source_turn, _context_turn()) if t is not None]
    if any(is_sensitive_turn(t) for t in turns):
        raise SensitiveRefused("this session holds a host's sensitive data: nothing from it "
                               "is written to what canopy knows about people")
    if by_agent is not None and not turns:
        raise SensitiveRefused("an agent's write must name the turn it comes from "
                               "(source turn, or X-Canopy-Parent-Turn), so a sensitive "
                               "session can be told apart")
