"""Tokens and cost per turn — from whatever record of the turn's model calls exists.

Three shapes reach canopy-web, one reader each, all landing in `TurnUsage`:

- A `claude -p` turn's uploaded transcript ends each run with a `{"type":
  "result"}` record carrying the run's `usage`, `total_cost_usd` and `modelUsage`.
  Summed per run (a credential failover runs it more than once). The only shape
  that REPORTS COST (`TurnUsage.runs > 0`).
- An interactive Claude Code turn's uploaded transcript has no `result`: each
  assistant record carries its own `message.usage`. Summed once per message id
  (Claude Code writes one record per content block, each repeating it).
- A chat session's rows (`persist_transcript_rows`) carry the same per-message
  usage on the first row of each assistant record (`canopy_transcript.record_usage`,
  the one reader of `message.usage`). Credited to the person's message they answer
  (`record_for_chat`).

Every path re-derives and upserts, so repeats are safe. A turn none of them saw
has no row — "not reported", never zero.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    runs: int = 0
    model_cost: dict[str, Decimal] = field(default_factory=dict)

    @property
    def model(self) -> str:
        """The model that cost the most — a turn may touch a small model for
        housekeeping, and the one doing the work is the one worth naming."""
        return max(self.model_cost, key=self.model_cost.get) if self.model_cost else ""


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def from_messages(per_message) -> Usage | None:
    """Sum `canopy_transcript.record_usage` dicts, once per message id (the last
    seen wins). None when there are none. Cost is not reported by these."""
    seen: dict[str, dict] = {}
    for i, u in enumerate(per_message):
        if u:
            seen[u.get("message_id") or f"#{i}"] = u
    if not seen:
        return None
    out = Usage()
    for u in seen.values():
        out.input_tokens += _int(u.get("input_tokens"))
        out.output_tokens += _int(u.get("output_tokens"))
        out.cache_read_tokens += _int(u.get("cache_read_tokens"))
        out.cache_creation_tokens += _int(u.get("cache_creation_tokens"))
        if u.get("model"):
            # No cost to rank models by: weight by output, the work that was done.
            out.model_cost[u["model"]] = out.model_cost.get(u["model"], Decimal(0)) + _int(u.get("output_tokens"))
    return out


def parse(raw: bytes | str) -> Usage | None:
    """A turn transcript's usage: its `result` records when it has any (a `claude
    -p` run, cost reported), else its assistant records' own usage (an
    interactive session). None when it has neither."""
    from canopy_transcript import record_usage

    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    out = Usage()
    init_model = ""
    per_message: list[dict] = []
    for line in text.splitlines():
        # Cheap pre-filter: transcripts are mostly tool output, and json-parsing
        # every line of a 100MB turn to find a few records would be the slow part.
        if '"result"' not in line and '"init"' not in line and '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "init":
            init_model = init_model or str(rec.get("model") or "")
            continue
        if rec.get("type") == "assistant":
            per_message.append(record_usage(rec))
            continue
        if rec.get("type") != "result":
            continue
        u = rec.get("usage") or {}
        out.runs += 1
        out.input_tokens += _int(u.get("input_tokens"))
        out.output_tokens += _int(u.get("output_tokens"))
        out.cache_read_tokens += _int(u.get("cache_read_input_tokens"))
        out.cache_creation_tokens += _int(u.get("cache_creation_input_tokens"))
        try:
            out.cost_usd += Decimal(str(rec.get("total_cost_usd") or 0))
        except ArithmeticError:
            pass
        for name, mu in (rec.get("modelUsage") or {}).items():
            cost = Decimal(str((mu or {}).get("costUSD") or 0)) if isinstance(mu, dict) else Decimal(0)
            out.model_cost[name] = out.model_cost.get(name, Decimal(0)) + cost
    if not out.runs:
        return from_messages(per_message)
    if not out.model_cost and init_model:
        out.model_cost[init_model] = out.cost_usd
    return out


def _store(ref: str, *, turn, workspace, agent, user, at, usage: Usage) -> None:
    """Upsert one TurnUsage. A row that came from `result` records (cost reported)
    is never replaced by a per-message estimate of the same turn."""
    from .models import TurnUsage

    if not usage.runs and TurnUsage.objects.filter(turn_ref=ref, runs__gt=0).exists():
        return
    TurnUsage.objects.update_or_create(
        turn_ref=ref,
        defaults=dict(
            turn=turn, workspace_id=workspace, agent=agent,
            agent_slug=agent.slug if agent is not None else "", user=user, at=at,
            model=usage.model[:100],
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=usage.cost_usd, runs=usage.runs,
        ),
    )


def record_for_turn(turn) -> bool:
    """(Re)derive and store one turn's usage from its transcript. Returns whether
    a row was written. Never raises — usage is never worth a failed append."""
    from apps.harness.ledger import read_transcript

    from . import activity

    try:
        usage = parse(read_transcript(turn))
        if usage is None:
            return False
        slug = turn.tenant_slug
        if not slug:
            return False
        _store(str(turn.pk), turn=turn, workspace=slug, agent=turn.acting_agent,
               user=activity.turn_actor(turn), at=turn.finished_at or turn.created_at, usage=usage)
        return True
    except Exception:  # noqa: BLE001
        logger.exception("could not derive usage for turn %s", turn.pk)
        return False


def record_for_chat(session, indices: list[int]) -> int:
    """Credit the usage carried by a chat session's newly stored rows (`indices`,
    their `turn_index`) to the person's message each answers: the latest user row
    before it. That message's turn when it has one (the same `TurnUsage` a turn
    transcript would write), else the message itself (someone typed straight into
    the session). Each affected answer is re-summed whole, so a re-ship or a later
    batch of the same answer is safe. Returns how many were written; never raises."""
    import bisect

    from apps.canopy_sessions.models import Message
    from apps.harness.models import Turn

    from . import activity

    try:
        users = list(Message.objects.filter(session=session, role=Message.USER)
                     .order_by("turn_index").values_list("turn_index", "source_turn_id", "author"))
        starts = [u[0] for u in users]
        anchors = {bisect.bisect_left(starts, i) - 1 for i in indices}
        written = 0
        for a in sorted(anchors):
            lo = starts[a] if a >= 0 else -1
            hi = starts[a + 1] if a + 1 < len(starts) else None
            rows = Message.objects.filter(session=session, turn_index__gt=lo, content__has_key="usage")
            if hi is not None:
                rows = rows.filter(turn_index__lt=hi)
            rows = list(rows.order_by("turn_index").values_list("content", "created_at"))
            usage = from_messages([(c or {}).get("usage") for c, _ in rows])
            if usage is None:
                continue
            turn_id = users[a][1] if a >= 0 else None
            turn = (Turn.objects.select_related("agent", "chat_session__agent", "initiator_user", "enqueued_by")
                    .filter(pk=turn_id).first() if turn_id else None)
            if turn is not None:
                ref, user, agent = str(turn.pk), activity.turn_actor(turn), turn.acting_agent
            else:
                author = (users[a][2] or {}) if a >= 0 else {}
                ref = f"chat:{session.pk.hex}:{lo}"
                user = get_user(author.get("user_id"))
                agent = session.agent
            _store(ref, turn=turn, workspace=session.workspace_id, agent=agent, user=user,
                   at=rows[-1][1], usage=usage)
            written += 1
        return written
    except Exception:  # noqa: BLE001
        logger.exception("could not record chat usage for session %s", session.pk)
        return 0


def get_user(user_id):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).first() if user_id else None
