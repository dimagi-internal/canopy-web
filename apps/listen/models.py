"""A listen session: one phone-to-laptop relay of end-to-end encrypted frames.

canopy-web only carries the frames (`consumers.ListenConsumer`); it never decrypts,
stores or logs them. This row holds what the relay needs to decide who may connect
and when the session ends: its owner and runner, the phone's PUBLIC keys (`hello`),
which socket holds each role, and byte totals. Design: docs/architecture/listen-relay.md.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

ROLES = ("phone", "laptop")


class ListenSession(models.Model):
    PAIR, MEETING = "pair", "meeting"
    KIND_CHOICES = [(PAIR, "Pair a phone"), (MEETING, "Meeting")]

    WAITING, LIVE, ENDED = "waiting", "live", "ended"
    STATE_CHOICES = [(WAITING, "Waiting"), (LIVE, "Live"), (ENDED, "Ended")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="listen_sessions")
    runner = models.ForeignKey("harness.Runner", on_delete=models.CASCADE, related_name="listen_sessions")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    #: Public keys only: {"phone_eph", "phone_key" (null for a pairing), "label"}.
    hello = models.JSONField(default=dict)
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=WAITING)
    end_reason = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    #: Totals only (phone -> laptop, laptop -> phone); nothing per frame is kept.
    bytes_up = models.BigIntegerField(default=0)
    bytes_down = models.BigIntegerField(default=0)
    # Which socket holds each role, and when it last said it was still there. A
    # web process that dies never releases its role, so a holder whose beat is
    # stale (`services.STALE_S`) counts as gone.
    phone_channel = models.CharField(max_length=200, blank=True, default="")
    laptop_channel = models.CharField(max_length=200, blank=True, default="")
    phone_seen_at = models.DateTimeField(null=True, blank=True)
    laptop_seen_at = models.DateTimeField(null=True, blank=True)
    #: When a role left a live session; the session ends 60 s later unless it is back.
    absent_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["runner", "state"]),
            models.Index(fields=["state", "created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"listen {self.kind} {self.pk} ({self.state})"
