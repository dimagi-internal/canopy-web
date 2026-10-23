"""An agent's close-out of a turn, and the turn list an agent page reads.

A turn row is the harness's (`Turn`): the runner creates it when it dispatches
work and finishes it when the work ends. The agent then posts a close-out
report (`POST /api/agents/{slug}/turns/`) — title, summary, task ids, work
products, transcript link — which this module attaches to that same row, or
records as a report-only row when nothing was dispatched. The list adds where
to see what each turn did (its chat session, its transcript, its share page).
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Exists, OuterRef
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding
from apps.session_sharing.models import ShareToken
from apps.workspaces import links as wslinks

from . import sensitivity
from .models import Turn, TurnTranscript


def _aware(value):
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.UTC)
    return value


def _claim_dispatch_row(agent: Agent, data) -> Turn | None:
    """The dispatch row this close-out belongs to, or None to create a fresh one.

    Match order, most specific first:
      1. cli_session_id — a turn already reported from this Claude session (re-run
         of the close-out). Also what the unique constraint keys on.
      2. session_key — the runner stamped the emdash session it created; the
         closing agent recovers the same name from its cwd. Newest UNREPORTED turn
         for that task wins, because a reused session serves many turns and the one
         being closed is the latest.

      3. cli_session_id AS the session key — a CLOUD runner has no emdash task:
         it stamps the turn with the Claude session id it ran (`finish`), and the
         closing agent, whose cwd is the shared agent clone rather than an emdash
         worktree, has no task name to recover. The Claude session id it reports
         is that same id, so it is the join. Without this every cloud close-out
         became its own report-only row beside the turn it was closing.

    No time window on (2): an agent turn legitimately runs for hours, and a wrong
    window would silently split one turn into two rows — the exact failure this
    merge exists to end. The `reported_at__isnull=True` filter is what keeps an
    older turn from being claimed twice.
    """
    if data.cli_session_id:
        existing = agent.turns.filter(cli_session_id=data.cli_session_id).first()
        if existing is not None:
            return existing
    task = getattr(data, "session_key", "") or data.cli_session_id or ""
    if task:
        return (
            agent.turns.filter(session_key=task, reported_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    return None


def upsert_turn(agent: Agent, data, initiator=None) -> Turn:
    """Attach an agent's close-out report to the turn it was dispatched as.

    Idempotent per (agent, cli_session_id). When no dispatch row can be matched —
    a turn a human started by hand in a terminal, or a fleet still posting without
    `session_key` — a report-only Turn is created instead, so the record is never
    dropped on the floor. That row carries origin=api and status=done because it is,
    from the harness's point of view, a turn that has already finished; it has no
    idempotency of its own to enforce, so the key is synthesized from the session.
    """
    fields = {
        "report_title": data.title,
        "report_summary": data.summary,
        "task_ext_ids": list(data.task_ext_ids),
        "work_product_urls": list(data.work_product_urls),
        "session_slug": data.session_slug,
        "share_token": data.share_token,
        "report_source": data.source,
        "cli_session_id": data.cli_session_id,
        "reported_at": timezone.now(),
    }
    extra_ref = dict(getattr(data, "origin_ref", None) or {})
    turn = _claim_dispatch_row(agent, data)
    if turn is not None:
        if sensitivity.is_sensitive(turn):
            # A report is the agent's words about what it read: a sensitive turn
            # keeps that there was one, never what it says.
            fields.update(report_title="", report_summary="", share_token="", session_slug="")
        for key, value in fields.items():
            setattr(turn, key, value)
        if extra_ref:
            turn.origin_ref = {**(turn.origin_ref or {}), **extra_ref}
        # The agent's own timings are better than the runner's: `started_at` from
        # the harness is when the SESSION was created, `ended_at` only the agent
        # knows. Never overwrite a known dispatch time with a null.
        if _aware(data.started_at):
            turn.started_at = _aware(data.started_at)
        if _aware(data.ended_at):
            turn.finished_at = _aware(data.ended_at)
        turn.save()
        return turn

    return Turn.objects.create(
        # Who reported it, when the caller says (the API passes the request's
        # asker). Provenance + the TURN_CREATED line come from the harness's
        # pre_save/post_save receivers (apps/harness/provenance.py).
        **(initiator.fields() if initiator is not None else {}),
        agent=agent,
        origin=Turn.ORIGIN_API,
        status=Turn.DONE,
        idempotency_key=f"closeout:{agent.slug}:{data.cli_session_id}",
        session_key=getattr(data, "session_key", "") or "",
        started_at=_aware(data.started_at),
        finished_at=_aware(data.ended_at),
        origin_ref=extra_ref,
        **fields,
    )


def stamp_share_urls(turns) -> list:
    """Stamp `share_url` on each turn: its transcript's page, under the
    workspace the transcript was shared from. One query for the whole page.
    Run AFTER redaction, so a hidden turn's blanked token yields no link."""
    tokens = {t.share_token for t in turns if t.share_token}
    homes = dict(
        ShareToken.objects.filter(token__in=tokens, revoked_at__isnull=True)
        .values_list("token", "session__workspace_id")
    ) if tokens else {}
    for t in turns:
        ws = homes.get(t.share_token) if t.share_token else None
        t.share_url = wslinks.scoped_url_or_none(ws, f"/share/{t.share_token}") if ws else None
    return turns


def list_turns(agent: Agent, limit: int = 100) -> list[Turn]:
    """Newest first. Turn.Meta orders ASC (the queue is drained oldest-first), which
    is the wrong end for a workspace timeline, so this reverses it explicitly.

    Each turn also carries where to go to see what it DID — `linked_session_id`
    (see `_link_turn_sessions`) and `has_transcript` — because a turn row on its
    own is only the dispatch envelope."""
    turns = list(
        agent.turns.select_related("agent")
        .annotate(has_transcript=Exists(TurnTranscript.objects.filter(turn=OuterRef("pk"))))
        .order_by("-created_at")[:limit]
    )
    _link_turn_sessions(agent, turns)
    return turns


# A runner names its session moments after the turn starts and reports the name
# at finish, so the session it created always exists by then. The slack covers a
# runner whose finish lands just before its session report does.
_SESSION_LINK_SLACK = dt.timedelta(minutes=5)


def _link_turn_sessions(agent: Agent, turns: list[Turn]) -> None:
    """Stamp `linked_session_id` on each turn: the chat that holds its actual work.

    Three cases, by how the turn ran:
    - a CHAT turn targets its session directly (`chat_session`);
    - an agent turn on a runner stamps `session_key` with the session it drove
      — an emdash task name on a LAPTOP, the Claude session id on a CLOUD runner
      — the same string the runner's record-session stores as
      `RunnerBinding.session_key`, so that is the join;
    - an older cloud turn (before the cloud runner recorded agent sessions) has
      no session; at most its transcript is on the turn (`has_transcript`).

    Session keys are emdash task names and names get reused, so a match must be a
    session that existed by the time the turn finished: a later session that
    happens to share the name is a different conversation. Of several that fit,
    the newest wins. One query for the whole page, not one per turn."""

    keys = {t.session_key for t in turns if t.session_key and not t.chat_session_id}
    by_key: dict[str, list[tuple[dt.datetime, object]]] = {}
    if keys:
        rows = RunnerBinding.objects.filter(
            session_key__in=keys, session__agent=agent
        ).values_list("session_key", "session__created_at", "session_id")
        for key, created_at, session_id in rows:
            by_key.setdefault(key, []).append((created_at, session_id))
    now = timezone.now()
    for t in turns:
        t.linked_session_id = t.chat_session_id
        if t.linked_session_id or not t.session_key:
            continue
        cutoff = (t.finished_at or now) + _SESSION_LINK_SLACK
        fits = [c for c in by_key.get(t.session_key, []) if c[0] <= cutoff]
        if fits:
            t.linked_session_id = max(fits, key=lambda c: c[0])[1]
