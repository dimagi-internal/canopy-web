"""Tokens per turn — from whatever record of the turn's model calls exists.

Three shapes reach canopy-web, one reader each, all landing in `TurnUsage`:

- A `claude -p` turn's uploaded transcript ends each run with a `{"type":
  "result"}` record carrying the run's `usage` and `modelUsage`. Summed per run
  (a credential failover runs it more than once); `TurnUsage.runs` counts them.
- An interactive Claude Code turn's uploaded transcript has no `result`: each
  assistant record carries its own `message.usage`. Summed once per message id
  (Claude Code writes one record per content block, each repeating it).
- A chat session's rows (`persist_transcript_rows`) carry the same per-message
  usage on the first row of each assistant record (`canopy_transcript.record_usage`,
  the one reader of `message.usage`, which also carries the record's timestamp).
  Credited to the person's message they answer (`record_for_chat`) — for an
  agent's chat and for a person's own session in a repository alike.

No cost: the only figure a run reports is `claude -p`'s `total_cost_usd`, which is
a list-price estimate that does not describe a subscription, so canopy does not
show one. Every path re-derives and upserts, so repeats are safe. A turn none of
them saw has no row — "not reported", never zero.
"""
from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    runs: int = 0
    #: When the last counted model call happened, when the record said.
    at: str = ""
    model_output: dict[str, int] = field(default_factory=dict)

    @property
    def model(self) -> str:
        """The model that wrote the most — a turn may touch a small model for
        housekeeping, and the one doing the work is the one worth naming."""
        return max(self.model_output, key=self.model_output.get) if self.model_output else ""


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def from_messages(per_message) -> Usage | None:
    """Sum `canopy_transcript.record_usage` dicts, once per message id (the last
    seen wins). None when there are none."""
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
        out.at = max(out.at, str(u.get("at") or ""))
        if u.get("model"):
            out.model_output[u["model"]] = out.model_output.get(u["model"], 0) + _int(u.get("output_tokens"))
    return out


def parse(raw: bytes | str) -> Usage | None:
    """A turn transcript's usage: its `result` records when it has any (a `claude
    -p` run), else its assistant records' own usage (an interactive session).
    None when it has neither."""
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
        for name, mu in (rec.get("modelUsage") or {}).items():
            if isinstance(mu, dict):
                out.model_output[name] = out.model_output.get(name, 0) + _int(mu.get("outputTokens"))
    if not out.runs:
        return from_messages(per_message)
    if not out.model_output and init_model:
        out.model_output[init_model] = out.output_tokens
    return out


def _when(usage: Usage, fallback):
    """The usage's own time (its last model call) when the record carried one, so
    history recovered later still counts on the day it happened; else `fallback`."""
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(usage.at) if usage.at else None
    return parsed or fallback


def _store(ref: str, *, turn, workspace, agent, user, at, usage: Usage, project: str = "") -> None:
    """Upsert one TurnUsage. A row from `result` records (the run's own totals) is
    never replaced by a per-message sum of the same turn."""
    from .models import TurnUsage

    if not usage.runs and TurnUsage.objects.filter(turn_ref=ref, runs__gt=0).exists():
        return
    TurnUsage.objects.update_or_create(
        turn_ref=ref,
        defaults=dict(
            turn=turn, workspace_id=workspace, agent=agent,
            agent_slug=agent.slug if agent is not None else "", project=project[:100],
            user=user, at=_when(usage, at), model=usage.model[:100],
            input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens, runs=usage.runs,
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
               user=activity.turn_actor(turn), at=turn.finished_at or turn.created_at, usage=usage,
               project=activity.repo_of(turn=turn))
        return True
    except Exception:  # noqa: BLE001
        logger.exception("could not derive usage for turn %s", turn.pk)
        return False


def record_for_chat(session, indices: list[int]) -> int:
    """Credit the usage carried by a session's newly stored rows (`indices`, their
    `turn_index`) to the person's message each answers: the latest user row before
    it. That message's turn when it has one (the same `TurnUsage` a turn transcript
    would write), else the message itself (someone typed straight into the
    session). Each affected answer is re-summed whole, so a re-ship or a later
    batch of the same answer is safe. Two queries per call however many answers a
    backfill touches. Returns how many were written; never raises."""
    from apps.canopy_sessions.models import Message
    from apps.harness.models import Turn

    from . import activity

    try:
        users = list(Message.objects.filter(session=session, role=Message.USER)
                     .order_by("turn_index").values_list("turn_index", "source_turn_id", "author"))
        starts = [u[0] for u in users]
        anchors = sorted({bisect.bisect_left(starts, i) - 1 for i in indices})
        if not anchors:
            return 0
        lo_all = starts[anchors[0]] if anchors[0] >= 0 else -1
        hi_last = starts[anchors[-1] + 1] if anchors[-1] + 1 < len(starts) else None
        spent = Message.objects.filter(session=session, turn_index__gt=lo_all, content__has_key="usage")
        if hi_last is not None:
            spent = spent.filter(turn_index__lt=hi_last)
        by_anchor: dict[int, list] = {}
        for index, content, created in spent.order_by("turn_index").values_list("turn_index", "content", "created_at"):
            by_anchor.setdefault(bisect.bisect_left(starts, index) - 1, []).append(((content or {}).get("usage"), created))
        turns = {t.pk: t for t in Turn.objects.select_related(
            "agent", "chat_session__agent", "initiator_user", "enqueued_by").filter(
            pk__in=[users[a][1] for a in anchors if a >= 0 and users[a][1]])}
        project = activity.repo_of(session=session)
        written = 0
        for a in anchors:
            rows = by_anchor.get(a)
            usage = from_messages([u for u, _ in rows]) if rows else None
            if usage is None:
                continue
            turn = turns.get(users[a][1]) if a >= 0 else None
            if turn is not None:
                ref, user, agent = str(turn.pk), activity.turn_actor(turn), turn.acting_agent
            else:
                author = (users[a][2] or {}) if a >= 0 else {}
                ref = f"chat:{session.pk.hex}:{starts[a] if a >= 0 else -1}"
                user = get_user(author.get("user_id")) or session.created_by
                agent = session.agent
            _store(ref, turn=turn, workspace=session.workspace_id, agent=agent, user=user,
                   at=rows[-1][1], usage=usage, project=project)
            written += 1
        return written
    except Exception:  # noqa: BLE001
        logger.exception("could not record chat usage for session %s", session.pk)
        return 0


def get_user(user_id):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_id).first() if user_id else None
