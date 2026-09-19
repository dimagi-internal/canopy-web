"""Web Push subscriptions.

Framework tier. This app observes apps.agents (never the reverse) — the same
direction apps.harness takes.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    """One browser we can reach. The browser hands us an opaque endpoint URL plus
    two keys; we POST encrypted payloads to that endpoint. One user has many
    (phone, laptop, a reinstall)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    # The browser re-sends the same endpoint on every subscribe() call, so this
    # is the identity — unique, and upserted rather than inserted (see services).
    endpoint = models.TextField(unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    # A subscription dies silently when the user uninstalls: the push service
    # starts returning 404/410. We prune on that, not on a timer.
    failure_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"push:{self.user_id}:{self.endpoint[-12:]}"


class AgentWaitingSnapshot(models.Model):
    """The last waiting_count we pushed about, per agent.

    The waiting set is a count (open items per agent) — there is no single event
    meaning "the fleet now needs you" — so we diff against this and push only on
    an INCREASE.
    Pushing on every recompute would buzz you when you CLEAR something, which is
    the fastest way to get notifications turned off.
    """

    agent = models.OneToOneField(
        "agents.Agent", on_delete=models.CASCADE, related_name="waiting_snapshot"
    )
    waiting_count = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"snapshot:{self.agent_id}:{self.waiting_count}"


class NotificationPreference(models.Model):
    """Per-user push knobs. No row means the defaults."""

    DEFAULT_SESSION_IDLE_MINUTES = 5

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference"
    )
    # How long a chat must stay quiet after a turn ends before "it's done" is
    # pushed. 0 turns that push off; a chat's `notify_every_completion` still
    # fires. Waiting (rather than pushing on every turn end) is what keeps an
    # agent's back-to-back turns from buzzing once each.
    session_idle_minutes = models.PositiveIntegerField(default=DEFAULT_SESSION_IDLE_MINUTES)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"notify:{self.user_id}:{self.session_idle_minutes}m"


def session_idle_minutes_for(user) -> int:
    pref = NotificationPreference.objects.filter(user=user).first()
    return pref.session_idle_minutes if pref else NotificationPreference.DEFAULT_SESSION_IDLE_MINUTES
