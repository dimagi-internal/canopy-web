"""Tokens and cost per turn, read from the turn's own uploaded transcript.

Every `claude -p` run ends with one `{"type": "result", ...}` record carrying the
whole run's `usage`, `total_cost_usd` and `modelUsage`. The runner forwards that
JSONL verbatim (`POST /turns/{id}/transcript`), so the server can derive usage
itself — no runner change, nothing to redeploy to a box. A turn that failed over
down the credential cascade has several runs, so every `result` is summed.

Derived when a batch carrying a `result` lands (`ledger.append_transcript`) and by
`manage.py backfill_activity` for history; both call `record_for_turn`, which
re-derives from the whole transcript and is safe to repeat. A turn whose
transcript never arrives has no row — "not reported", never zero.
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


def parse(raw: bytes | str) -> Usage | None:
    """Sum every `result` record in a JSONL transcript. None when there is none."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    out = Usage()
    init_model = ""
    for line in text.splitlines():
        # Cheap pre-filter: transcripts are mostly tool output, and json-parsing
        # every line of a 100MB turn to find two records would be the slow part.
        if '"result"' not in line and '"init"' not in line:
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
        return None
    if not out.model_cost and init_model:
        out.model_cost[init_model] = out.cost_usd
    return out


def record_for_turn(turn) -> bool:
    """(Re)derive and store one turn's usage from its transcript. Returns whether
    a row was written. Never raises — usage is never worth a failed append."""
    from apps.harness.ledger import read_transcript

    from . import activity
    from .models import TurnUsage

    try:
        usage = parse(read_transcript(turn))
        if usage is None:
            return False
        slug = turn.tenant_slug
        if not slug:
            return False
        agent = turn.acting_agent
        TurnUsage.objects.update_or_create(
            turn_ref=str(turn.pk),
            defaults=dict(
                turn=turn, workspace_id=slug, agent=agent,
                agent_slug=agent.slug if agent is not None else "",
                user=activity.turn_actor(turn),
                at=turn.finished_at or turn.created_at,
                model=usage.model[:100],
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cost_usd=usage.cost_usd, runs=usage.runs,
            ),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("could not derive usage for turn %s", turn.pk)
        return False
