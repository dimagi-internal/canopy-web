"""A durable, fleet-wide log of actions and errors.

Framework tier. Generic over its producer on PURPOSE: ``source`` and ``kind``
are strings, never FKs — the same discipline ``Item`` and ``feedback`` follow.
An FK would mean one integration per producer and would break the one-way
framework→product rule the moment a product app wanted to log something.

**This is a log, not a queue.** It deliberately emits no signal: no ``Item``, no
Web Push, no timeline event (``tests/test_events_emits_nothing.py`` guards
that). A fault is not a decision — ``Item``'s closed set
(``implement``/``skip``/``defer``) does not describe "transcript flush failed
400 times", and ``Item`` count increases drive Web Push, so a flapping runner
would become a notification storm. A turn reads the pool when its owner is
ready and decides what deserves acting on.

Why it exists: before this, an operational fault had nowhere durable to go.
``TurnEvent`` hangs off a ``Turn``, so a fault with no turn (a Gmail watch
expired, a Pub/Sub subscription misconfigured) was unrepresentable.
``MCPAuditLog`` is per-MCP-call. ``/timeline`` is a derived view over live
models, so something must exist before it can be shown. And the runner's
``failure_log`` is an in-memory dict writing to a laptop logfile — a week of
failing stream posts was invisible to the server.
"""
from __future__ import annotations

from django.db import models


class Event(models.Model):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    LEVEL_CHOICES = [(INFO, "Info"), (WARN, "Warn"), (ERROR, "Error")]

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="events",
    )
    """NOT NULL, deliberately. A nullable tenant FK in a read predicate is a
    known bug class here: six predicates independently grew a
    ``workspace_id IS NULL`` leg meaning *allow*, and ``agents/0013``
    constrained the column precisely to end it. Every producer supplies one; a
    PAT caller gets its default. There is no NULL-means-visible leg to add."""

    source = models.CharField(max_length=64)
    """The producing subsystem: ``inbound.gmail``, ``runner.stream``,
    ``harness.claim``. A string so this app never imports its producers."""

    kind = models.CharField(max_length=64)
    """What happened: ``gmail.push``, ``gmail.push.missed``,
    ``gmail.watch.expiring``. Dotted, narrowing left to right, so a caller can
    filter a family with ``startswith`` without a taxonomy table."""

    level = models.CharField(max_length=8, choices=LEVEL_CHOICES, default=INFO)
    """``info`` carries ACTIONS, which is why this is an event log and not an
    error log — "push received and rung 2 runners" is worth having when you are
    reconstructing why something did or did not happen."""

    key = models.CharField(max_length=200, blank=True, default="")
    """The coalescing key. A repeat of ``(workspace, source, key)`` bumps
    ``count`` and ``last_seen_at`` instead of inserting — ``failure_log``'s
    streak idea made durable. Blank NEVER coalesces (see the constraint below):
    two independent actions are two events, not a duplicate."""

    summary = models.CharField(max_length=500, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)

    count = models.PositiveIntegerField(default=1)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen_at"]
        indexes = [
            models.Index(fields=["workspace", "-last_seen_at"]),
            models.Index(fields=["workspace", "source", "kind"]),
            models.Index(fields=["workspace", "level", "-last_seen_at"]),
        ]
        constraints = [
            # Blank key is EXEMPT — the same partial-index shape `feedback` uses
            # for `source_ref`. A keyed repeat is one ongoing fault; two blank
            # rows are two things that happened.
            models.UniqueConstraint(
                fields=["workspace", "source", "key"],
                condition=~models.Q(key=""),
                name="uniq_event_workspace_source_key",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"event:{self.source}:{self.kind}:{self.pk}"
