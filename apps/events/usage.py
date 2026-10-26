"""Tokens: what each box's Claude Code sessions spent, as the box reports it — the
ONE path tokens reach canopy-web.

A box scans its own transcripts in the repos canopy names for it (`scope`) with
`canopy_transcript.scan_usage` and posts per-session, per-UTC-day totals, no content
(`POST /runners/{id}/usage` → `record`). Each session is matched to canopy's own
record of that Claude session (`_match`) and attributed from it — its workspace,
agent or repository, and person. One canopy has no record of — a session the
person's machine never sent up — is not kept: canopy counts only the sessions it
was sent, and never reaches down for the others.

No cost: the only figure a run reports is `claude -p`'s list-price estimate, which
does not describe a subscription.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import SessionUsage

logger = logging.getLogger(__name__)

_TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")


def scope(runner) -> list[str]:
    """The repos canopy knows for this box — the only ones it scans: the projects
    it declares, and the agents it serves (by slug and by repo name)."""
    from apps.agents.credentials.delegations import agent_repo
    from apps.harness.routing import agents_routed_to

    names = set(runner.project_names())
    for agent in agents_routed_to(runner):
        names.add(agent.slug)
        repo = agent_repo(agent)
        if repo:
            names.add(repo.split("/", 1)[1])
    return sorted(n for n in names if n)


def _match(claude_session: str):
    """(canopy Session, Turn) that recorded this Claude session, either may be None:
    a binding's `transcript_id` (laptop sessions), a turn's `session_key` or
    `cli_session_id` (cloud turns and agents' close-outs)."""
    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Turn

    binding = (RunnerBinding.objects.select_related("session", "session__agent", "session__created_by")
               .filter(transcript_id=claude_session).first())
    turn = (Turn.objects.select_related("agent", "chat_session", "chat_session__agent",
                                        "initiator_user", "enqueued_by")
            .filter(session_key=claude_session).order_by("-created_at").first()
            or Turn.objects.select_related("agent", "chat_session", "chat_session__agent",
                                           "initiator_user", "enqueued_by")
            .filter(cli_session_id=claude_session).order_by("-created_at").first())
    session = binding.session if binding is not None else (turn.chat_session if turn else None)
    return session, turn


def _attribution(runner, repo: str, session, turn) -> dict | None:
    """Whose this usage is, or None when canopy has no record of it. A turn says it
    best (its tenant, agent, and the person who asked); a session next (its
    workspace, agent, creator)."""
    from . import activity

    if turn is not None and turn.tenant_slug:
        agent = turn.acting_agent
        return {"workspace_id": turn.tenant_slug, "session": session, "agent": agent,
                "project": "" if agent else (activity.repo_of(turn=turn) or repo),
                "user": activity.turn_actor(turn) or getattr(runner, "owner", None)}
    if session is not None:
        return {"workspace_id": session.workspace_id, "session": session, "agent": session.agent,
                "project": "" if session.agent_id else (session.project or repo),
                "user": session.created_by or getattr(runner, "owner", None)}
    return None


def _dt(value: str):
    return parse_datetime(value) if value else None


def record(runner, sessions: list[dict]) -> dict:
    """Store a box's report: each session's days replace what was held for it. A
    session canopy has no record of is dropped, never kept."""
    stored = matched = 0
    with transaction.atomic():
        for s in sessions:
            cs = str(s.get("claude_session") or "")[:64]
            days = [d for d in (s.get("days") or []) if d.get("day")]
            if not cs or not days:
                continue
            session, turn = _match(cs)
            repo = str(s.get("repo") or "")[:100]
            who = _attribution(runner, repo, session, turn)
            if who is None:
                continue
            matched += 1
            agent = who["agent"]
            common = dict(
                runner=runner, repo=repo,
                first_at=_dt(s.get("first_at") or ""), last_at=_dt(s.get("last_at") or ""),
                workspace_id=who["workspace_id"], session=who["session"], agent=agent,
                agent_slug=agent.slug if agent is not None else "", project=(who["project"] or "")[:100],
                user=who["user"],
            )
            kept = []
            for d in days:
                day = dt.date.fromisoformat(str(d["day"])[:10])
                kept.append(day)
                SessionUsage.objects.update_or_create(
                    claude_session=cs, day=day,
                    defaults={**common, **{k: int(d.get(k) or 0) for k in _TOKENS},
                              "messages": int(d.get("messages") or 0), "model": str(d.get("model") or "")[:100]})
                stored += 1
            SessionUsage.objects.filter(claude_session=cs).exclude(day__in=kept).delete()
        type(runner).objects.filter(pk=runner.pk).update(usage_reported_at=timezone.now())
    return {"stored": stored, "matched": matched}
