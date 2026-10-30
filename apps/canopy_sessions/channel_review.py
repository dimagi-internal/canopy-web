"""Letting an agent review the sessions that live in one Slack channel.

A support channel is where people show their canopy sessions to be helped; the agent
that answers there (a coach) should not need each poster to enrol one by one. An owner
or admin of the agent's workspace gives it the channel's sessions instead
(`SessionChannelReview`, leg 8 of `access.visible_session_q`) — from the channel
allow-list on the Slack settings page (`apps/slack/ingest.allow`), which is the only
caller. Read-only, never a sensitive session, nothing outside that channel.

Each grant and revoke is on the agent's workspace event log (`lifecycle.SHARING_CHANGED`).
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from . import lifecycle
from .models import SessionChannelReview


def live(agent):
    return SessionChannelReview.objects.filter(agent=agent, revoked_at__isnull=True)


def is_on(agent, *, slack_team: str, channel_id: str) -> bool:
    return live(agent).filter(slack_team=slack_team, channel_id=channel_id).exists()


def grant(agent, *, slack_team: str, channel_id: str, channel_name: str = "", by) -> SessionChannelReview:
    """Give `agent` the sessions of one channel. Granting again is a no-op."""
    with transaction.atomic():
        current = live(agent).filter(slack_team=slack_team, channel_id=channel_id).first()
        if current is not None:
            return current
        row = SessionChannelReview.objects.create(
            agent=agent, slack_team=slack_team, channel_id=channel_id, channel_name=channel_name,
            granted_by=by if getattr(by, "pk", None) else None)
        _changed(agent, row, by=by, granted=True)
    return row


def revoke(agent, *, slack_team: str, channel_id: str, by) -> bool:
    """Take the channel's sessions back from `agent`. False when it had none."""
    with transaction.atomic():
        rows = list(live(agent).filter(slack_team=slack_team, channel_id=channel_id))
        for row in rows:
            row.revoked_at = timezone.now()
            row.save(update_fields=["revoked_at"])
            _changed(agent, row, by=by, granted=False)
    return bool(rows)


def _changed(agent, row: SessionChannelReview, *, by, granted: bool) -> None:
    lifecycle.record(lifecycle.SHARING_CHANGED, None, by=by if getattr(by, "pk", None) else None,
                     workspace_id=agent.workspace_id,
                     change={"field": "channel_review", "agent": agent.slug, "channel": row.channel_id,
                             "before": not granted, "after": granted})
