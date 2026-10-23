"""Tokens: what each box's Claude Code sessions spent, as the box reports it — the
ONE path tokens reach canopy-web.

A box scans its own transcripts in the repos canopy names for it (`scope`) with
`canopy_transcript.scan_usage` and posts per-session, per-UTC-day totals, no content
(`POST /runners/{id}/usage` → `record`). Each session is matched to canopy's own
record of that Claude session (`_match`) and attributed from it — its workspace,
agent or repository, and person. One that matches nothing is MISSING: kept without
a workspace, visible only to the box's admins (`missing`), until they send it up
(`send_up`), which files it as a session and asks the box for its transcript.

A sensitive-data box (`runner_requirements.is_sensitive_data_box`) reports what was
spent and nothing else: no title, no missing session, and nothing is ever sent up
from it. A session canopy has no record of there is a confined turn's leftover (an
earlier credential's attempt, a crash), and filing it would make the box ship words
canopy keeps sealed as an ordinary transcript.

No cost: the only figure a run reports is `claude -p`'s list-price estimate, which
does not describe a subscription.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.db import transaction
from django.db.models import Max, Min, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.harness import runner_requirements, sensitivity

from .models import SessionUsage

logger = logging.getLogger(__name__)


class SendUpRefusedError(Exception):
    """This box's missing sessions are never sent up (a sensitive-data box)."""


SENSITIVE_BOX_REFUSAL = (
    "This runner is a sensitive-data box: it keeps its transcripts to itself, so its "
    "sessions are never sent up. A session canopy has no record of there is a confined "
    "turn's leftover, and the box deletes it.")

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


def _attribution(runner, repo: str, session, turn) -> dict:
    """Whose this usage is. A turn says it best (its tenant, agent, and the person
    who asked); a session next (its workspace, agent, creator); nothing → missing."""
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
    return {"workspace_id": None, "session": None, "agent": None, "project": repo,
            "user": getattr(runner, "owner", None)}


def _dt(value: str):
    return parse_datetime(value) if value else None


def record(runner, sessions: list[dict], *, full: bool = False) -> dict:
    """Store a box's report: each session's days replace what was held for it.
    `full` (a requested rescan, on its last chunk) settles the ask."""
    stored = matched = 0
    # The box scans only `scope`, but the server holds the line too: a session
    # canopy has no record of, in a repo its owner has not opted into, is not kept.
    allowed = set(scope(runner))
    sensitive_box = runner_requirements.is_sensitive_data_box(runner)
    with transaction.atomic():
        for s in sessions:
            cs = str(s.get("claude_session") or "")[:64]
            days = [d for d in (s.get("days") or []) if d.get("day")]
            if not cs or not days:
                continue
            session, turn = _match(cs)
            repo = str(s.get("repo") or "")
            if session is None and turn is None and (
                    sensitive_box or (repo not in allowed and not runner.opts_in(repo))):
                # A sensitive-data box's session canopy has no record of is never
                # kept: kept, it would be listed as missing and could be sent up.
                continue
            who = _attribution(runner, str(s.get("repo") or "")[:100], session, turn)
            # A session's first prompt is its title here; a sensitive one's is the
            # host's data (spec 2026-10-22-sensitive-data-agents §4).
            # A sensitive-data box's titles are blank whatever the session: the box
            # holds only sensitive work.
            title = ("" if sensitive_box or sensitivity.any_sensitive(session, turn)
                     else str(s.get("title") or "")[:200])
            matched += who["workspace_id"] is not None
            agent = who["agent"]
            common = dict(
                runner=runner, repo=str(s.get("repo") or "")[:100], title=title,
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
        now = timezone.now()
        fields = {"usage_reported_at": now}
        if full:
            fields.update(usage_scan_requested_at=None, usage_scanned_at=now)
        type(runner).objects.filter(pk=runner.pk).update(**fields)
    return {"stored": stored, "matched": matched}


def missing(runner) -> list[dict]:
    """This box's sessions canopy has no record of, one row each, newest first.
    Never any on a sensitive-data box."""
    if runner_requirements.is_sensitive_data_box(runner):
        return []
    rows = (SessionUsage.objects.filter(runner=runner, workspace__isnull=True)
            .values("claude_session", "repo", "title")
            .annotate(first_at=Min("first_at"), last_at=Max("last_at"), days=Sum(1),
                      **{k: Sum(k) for k in _TOKENS})
            .order_by("-last_at"))
    return [{**r, **{k: int(r[k] or 0) for k in _TOKENS}} for r in rows]


def send_up(runner, claude_sessions: list[str]) -> list[str]:
    """File each missing session as a canopy session and ask its box for the
    transcript (the ordinary backfill, keyed `claude:<id>`); its usage is matched
    to it at once. Filed where the report sweep files that box's sessions
    (`harness.session_sync.session_home`). Returns the canopy session ids.
    Raises SendUpRefusedError for a sensitive-data box, whatever is asked."""
    from apps.canopy_sessions import transcript_read as session_transcript_read
    from apps.canopy_sessions.models import RunnerBinding, Session
    from apps.harness.session_sync import session_home

    if runner_requirements.is_sensitive_data_box(runner):
        raise SendUpRefusedError(SENSITIVE_BOX_REFUSAL)
    out = []
    pending = {r["claude_session"]: r for r in missing(runner) if r["claude_session"] in set(claude_sessions)}
    for cs, row in pending.items():
        filed = session_home(runner, row["repo"])
        if filed is None:
            continue  # another tree's agent's work: never filed (session_home)
        with transaction.atomic():
            agent, project, workspace = filed
            session = Session.objects.create(
                workspace=workspace, agent=agent, project=project, origin=Session.ORIGIN_RUNNER,
                created_by=getattr(runner, "owner", None), title=row["title"] or cs)
            RunnerBinding.objects.create(
                session=session, runner=runner, session_key=f"claude:{cs}", transcript_id=cs,
                emdash_project=agent.slug if agent else project, host=runner.host)
            who = _attribution(runner, row["repo"], session, None)
            SessionUsage.objects.filter(claude_session=cs).update(
                workspace_id=who["workspace_id"], session=session, agent=agent,
                agent_slug=agent.slug if agent else "", project=who["project"] or "", user=who["user"])
        session_transcript_read.request_backfill(session)
        out.append(str(session.pk))
    return out
