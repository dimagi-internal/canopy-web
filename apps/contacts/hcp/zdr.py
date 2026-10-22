"""Zero data retention: nothing from a ZDR session is ever written."""
from __future__ import annotations

import uuid

# --- zero data retention: nothing from a ZDR session is ever written ------------------
#
# Owner rule (Jonathan, 2026-10-08): "if we are talking to a zdr runner, nothing should
# ever be written to this from those sessions." A ZDR conversation exists so that what is
# said in it is not kept; an entry, or an agent's inference from it, would keep it here.
#
# A turn is ZDR when its conversation REQUIRES ZDR (the host's signed requirement,
# `harness.runner_requirements`) or it was CLAIMED by a runner whose owner declared
# `zdr` (`RunnerFlag`) — and so is any turn descended from one (`parent_turn`), since a
# dispatch carries the session's words onward. Fail closed: a malformed requirement
# counts as ZDR, and an AGENT write that names no turn at all is refused, because it
# cannot be shown not to come from one.

ZDR = "zdr"
_ANCESTRY_MAX = 20


class ZdrRefused(Exception):
    """A write from a zero-data-retention session. Safe to show the caller."""


def _turn_is_zdr(turn) -> bool:
    from apps.harness import runner_requirements as rr
    from apps.harness.models import RunnerFlag

    reqs = rr.requirements_of(turn)
    if ZDR in reqs or rr.UNSATISFIABLE in reqs:
        return True
    return bool(turn.claimed_by_id) and RunnerFlag.objects.filter(
        runner_id=turn.claimed_by_id, flag=ZDR).exists()


def is_zdr_turn(turn) -> bool:
    """Is this turn — or any turn it descends from — a ZDR session's?"""
    seen = set()
    while turn is not None and turn.pk not in seen and len(seen) < _ANCESTRY_MAX:
        seen.add(turn.pk)
        if _turn_is_zdr(turn):
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


def refuse_if_zdr(*, source_turn=None, by_agent=None) -> None:
    """Raise ZdrRefused when this write comes from a ZDR session — or is an agent's write
    that cannot say which session it comes from."""
    turns = [t for t in (source_turn, _context_turn()) if t is not None]
    if any(is_zdr_turn(t) for t in turns):
        raise ZdrRefused("this session runs under zero data retention: nothing from it "
                         "is written to what canopy knows about people")
    if by_agent is not None and not turns:
        raise ZdrRefused("an agent's write must name the turn it comes from "
                         "(source turn, or X-Canopy-Parent-Turn), so a zero-data-retention "
                         "session can be told apart")
