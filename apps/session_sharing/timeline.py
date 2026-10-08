"""Timeline source for shared Claude Code sessions.

Link-visible sessions deep-link to the public ``/w/<ws>/share/<token>`` viewer; private
(dimagi-only) sessions fall back to the ``/sessions`` list (there's no per-session
owner route).
"""
from __future__ import annotations

import datetime as dt

from django.db.models import Prefetch

from apps.workspaces.services import scoped_path_or_none

from .models import Session, ShareToken


def recent_events(*, limit: int, before: dt.datetime | None, user) -> list:
    from apps.timeline.types import ActivityEvent, actor_name, cursor_page

    # Prefetch active share tokens (newest first) so the link-out href doesn't
    # fire a per-row active_token() query — one extra query for the whole page.
    active_tokens = Prefetch(
        "share_tokens",
        queryset=ShareToken.objects.filter(revoked_at__isnull=True).order_by("-created_at"),
        to_attr="active_tokens",
    )
    qs = (
        Session.objects.select_related("owner")
        .prefetch_related(active_tokens)
        .order_by("-created_at")
    )
    out: list[ActivityEvent] = []
    for s in cursor_page(qs, "created_at", before=before, limit=limit):
        href = "/sessions"
        if s.visibility == Session.VISIBILITY_LINK and s.active_tokens:
            href = scoped_path_or_none(s.workspace_id, f"/share/{s.active_tokens[0].token}") or href
        out.append(
            ActivityEvent(
                subsystem="sessions",
                kind="session",
                at=s.created_at,
                title=s.title or "(untitled session)",
                project_slug=s.project_slug,
                actor=actor_name(s.owner),
                href=href,
                id=f"session:{s.slug}",
                icon="session",
            )
        )
    return out
