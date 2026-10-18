"""Seed the analytics fact log from the history that still exists.

    manage.py backfill_activity [--days 365]

Idempotent: every fact carries the `ref` its live producer uses (a turn, session,
setting change or MCP call id), and a (kind, ref) already present is skipped — so a
re-run, or a run after the live producers are deployed, never double-counts.
Tokens are not here: each box reports them from its own transcripts
(`events.usage`). Facts recorded as a person
whose login turns out to be an agent's or a system account's are reclassified.
"""
from __future__ import annotations

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events import activity
from apps.events.models import Activity, SettingChange


class Command(BaseCommand):
    help = "Backfill Activity facts from existing turns, chats, setting changes and MCP calls."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365)

    def handle(self, *args, days: int, **opts):
        from apps.canopy_sessions.models import Session
        from apps.harness.models import Turn
        from apps.mcp.models import MCPAuditLog

        since = timezone.now() - dt.timedelta(days=days)
        have = set(Activity.objects.filter(at__gte=since - dt.timedelta(days=1))
                   .values_list("kind", "ref"))
        written = {k: 0 for k in activity.KINDS}

        def once(kind: str, ref: str, fn) -> None:
            if (kind, ref) in have:
                return
            if fn() is not None:
                have.add((kind, ref))
                written[kind] += 1

        turns = (Turn.objects.filter(created_at__gte=since)
                 .select_related("agent", "chat_session", "chat_session__agent",
                                 "initiator_user", "enqueued_by")
                 .order_by("created_at"))
        for t in turns.iterator(chunk_size=500):
            ref = str(t.pk)
            once(activity.TURN_ENQUEUED, ref, lambda: activity.for_turn(
                activity.TURN_ENQUEUED, t, at=t.created_at))
            if t.status in Turn.TERMINAL and t.finished_at:
                once(activity.TURN_FINISHED, ref, lambda: activity.for_turn(
                    activity.TURN_FINISHED, t, at=t.finished_at))
            # A person's send in a chat is a turn on that session with a person behind it.
            if t.chat_session_id and activity.turn_actor(t) is not None:
                once(activity.CHAT_MESSAGE, ref, lambda: activity.emit(
                    activity.CHAT_MESSAGE, workspace=t.chat_session.workspace_id,
                    actor=activity.turn_actor(t), agent=t.chat_session.agent, surface=t.origin,
                    ref=ref, at=t.created_at, project=activity.repo_of(session=t.chat_session)))

        for s in (Session.objects.filter(created_at__gte=since)
                  .select_related("agent", "created_by").iterator(chunk_size=500)):
            once(activity.CHAT_SESSION_CREATED, str(s.pk), lambda: activity.emit(
                activity.CHAT_SESSION_CREATED, workspace=s.workspace_id, actor=s.created_by,
                agent=s.agent, surface=s.origin, ref=str(s.pk), at=s.created_at,
                project=activity.repo_of(session=s)))

        for c in (SettingChange.objects.filter(created_at__gte=since, workspace__isnull=False)
                  .select_related("actor").iterator(chunk_size=500)):
            ref = f"sc{c.pk}"
            once(activity.SETTING_CHANGED, ref, lambda: activity.emit(
                activity.SETTING_CHANGED, workspace=c.workspace_id, actor=c.actor, surface=c.via,
                ref=ref, meta={"operation": c.operation}, at=c.created_at))

        for m in (MCPAuditLog.objects.filter(created_at__gte=since).exclude(workspace_slug="")
                  .select_related("user").iterator(chunk_size=500)):
            ref = f"mcp{m.pk}"
            once(activity.MCP_CALL, ref, lambda: activity.emit(
                activity.MCP_CALL, workspace=m.workspace_slug, actor=m.user, surface="mcp",
                ref=ref, meta={"tool": m.tool, "ok": m.ok}, at=m.created_at))

        reclassified = activity.reclassify()
        repos = self._fill_repositories(since)
        for kind, n in written.items():
            self.stdout.write(f"{kind}: {n} written")
        self.stdout.write(f"facts reclassified as an agent or system: {reclassified}")
        self.stdout.write(f"facts given their repository: {repos}")

    def _fill_repositories(self, since) -> int:
        """Name the repository on facts recorded before facts carried one: repo
        turns, and sessions no agent runs (`activity.repo_of`)."""
        from apps.canopy_sessions.models import Session
        from apps.harness.models import Turn

        n = 0
        for t in (Turn.objects.filter(created_at__gte=since, agent__isnull=True)
                  .select_related("chat_session").iterator(chunk_size=500)):
            repo = activity.repo_of(turn=t)
            if repo:
                n += Activity.objects.filter(ref=str(t.pk), project="").update(project=repo)
        for s in Session.objects.filter(agent__isnull=True).exclude(project="").iterator(chunk_size=500):
            n += Activity.objects.filter(kind=activity.CHAT_SESSION_CREATED, ref=str(s.pk), project="").update(project=s.project)
        return n
