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

from django.conf import settings
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


class SettingChange(models.Model):
    """One change to a setting — who made it, through what, and what changed.

    Written by ONE plumbing, `apps/events/audit.py`, which wraps every REST route
    `apps/api/route_gates.py::SETTINGS` names. The web UI, the `canopy` CLI, a
    script and an MCP tool all reach a setting through those routes (every MCP
    tool IS a route, called in-process), so no entry point can change a setting
    without leaving a row. A new write route cannot ship until it is classified
    as a setting or not (`tests/test_every_route_declares_its_gate.py`).

    Origin, 2026-10-12: ACE carried a strict routing rule nobody could account
    for. Routing rows carry no author, each save deletes and re-creates them, and
    only MCP-originated writes left even a tool-call line (`MCPAuditLog`).

    Append-only: there is no route that edits or deletes a row.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="setting_changes",
    )
    """The tenant whose setting changed. NULL only for a PERSON's own settings
    (their tokens, preferences, memory grants), which belong to no tenant. NULL
    never widens visibility: a NULL row is readable by its actor and nobody
    else (`audit.visible_to`). It is not the NULL-means-allow leg that `Event`
    refuses — read predicates here never treat it as "any workspace"."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="setting_changes",
    )
    actor_email = models.CharField(max_length=254, blank=True, default="")
    """Kept beside the FK so the row still names the person after the account
    is gone."""

    via = models.CharField(max_length=16)
    """How the change arrived: ``ui`` (browser session), ``mcp`` (an MCP tool),
    ``cli`` (the canopy CLI), ``api`` (any other token holder), ``runner`` (a
    runner daemon). From the credential and the MCP flag — the credential is
    verified; ``client``/``user_agent`` are only what the caller claimed."""

    credential = models.JSONField(default=dict, blank=True)
    """The credential's type, id and label (never its secret)."""

    client = models.CharField(max_length=200, blank=True, default="")
    user_agent = models.CharField(max_length=200, blank=True, default="")
    mcp_tool = models.CharField(max_length=128, blank=True, default="")
    request_id = models.CharField(max_length=128, blank=True, default="")

    operation = models.CharField(max_length=128)
    """The route's operationId (also its MCP tool name): ``set_agent_actor_route``."""
    method = models.CharField(max_length=8)
    path = models.CharField(max_length=500)
    target = models.CharField(max_length=300, blank=True, default="")
    """What was changed, in a person's words: ``ace``, ``ace / route for x@y``."""
    summary = models.CharField(max_length=500)

    request = models.JSONField(default=dict, blank=True)
    """Path arguments and the request body, with secrets redacted
    (`audit.redact`)."""

    changes = models.JSONField(default=list, blank=True)
    """Before/after, when the route's handler reported them
    (`audit.note_change`): ``[{"scope", "before", "after"}]``. Empty when the
    handler doesn't — the request body is then the record of what was set."""

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["workspace", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["operation", "-created_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"setting-change:{self.operation}:{self.pk}"


class Activity(models.Model):
    """One thing that happened, as an analytics FACT — append-only, never coalesced.

    THE table analytics reads (`apps/events/analytics.py`), written by ONE helper,
    `apps.events.activity.emit`. `Event` above is the operational log: it coalesces
    repeats into one row with a count, which is right for "still broken after 400
    tries" and wrong for counting, so facts live here instead.

    The audit tables (`SettingChange`, `MCPAuditLog`, `AgentCredentialRead`, …) stay
    the record of what they audit; their writers ALSO emit a fact here, so "who did
    anything in this workspace, when" is one indexed query, not a join across every
    producer. Retention does not touch this table: it holds no content, only who,
    where, what kind and when (`docs/architecture/analytics.md`).
    """

    USER, AGENT, CONTACT, RUNNER, SYSTEM = "user", "agent", "contact", "runner", "system"
    ACTOR_KINDS = [(USER, "User"), (AGENT, "Agent"), (CONTACT, "Contact"),
                   (RUNNER, "Runner"), (SYSTEM, "System")]

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="activity",
    )
    """NOT NULL for the same reason as `Event.workspace`: no NULL-means-visible leg."""

    at = models.DateTimeField()
    kind = models.CharField(max_length=64)
    """Dotted, narrowing left to right (`turn.enqueued`, `chat.message`,
    `setting.changed`) — the vocabulary is `activity.KINDS`."""

    actor_kind = models.CharField(max_length=8, choices=ACTOR_KINDS, default=SYSTEM)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="activity",
    )
    """The PERSON, when one did it. Set for actor_kind `user`; for an agent acting
    with its own login it is that login, so per-person tables can exclude agents
    by `actor_kind`."""

    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.SET_NULL, null=True, blank=True, related_name="activity",
    )
    agent_slug = models.CharField(max_length=64, blank=True, default="")
    """Kept beside the FK so the fact still names the agent after it is deleted."""

    surface = models.CharField(max_length=32, blank=True, default="")
    """Where it arrived from: `ui`, `mcp`, `cli`, `slack`, `email`, `scheduler`, …"""

    ref = models.CharField(max_length=64, blank=True, default="")
    """The id of the thing it is about (a turn, a session), for drill-down and so
    a backfill can skip facts it already wrote (`activity.emit(..., dedupe=True)`)."""

    meta = models.JSONField(default=dict, blank=True)
    """Small and non-sensitive: a status, an origin. Never content, never a secret."""

    class Meta:
        ordering = ["-at"]
        indexes = [
            models.Index(fields=["workspace", "at"]),
            models.Index(fields=["workspace", "kind", "at"]),
            models.Index(fields=["actor", "at"]),
            models.Index(fields=["kind", "ref"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"activity:{self.kind}:{self.pk}"


class TurnUsage(models.Model):
    """Tokens and cost one turn spent, from the `result` record(s) of its uploaded
    `claude -p` transcript (`apps/events/usage.py`).

    Its own row, not columns on `Turn`, and not cascaded from the transcript:
    retention purges transcripts, and this is the part of them analytics needs to
    keep. It carries its own workspace/agent/person so it outlives the turn.
    """

    turn = models.OneToOneField(
        "harness.Turn", on_delete=models.SET_NULL, null=True, blank=True, related_name="usage",
    )
    turn_ref = models.CharField(max_length=64, unique=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="turn_usage",
    )
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.SET_NULL, null=True, blank=True, related_name="turn_usage",
    )
    agent_slug = models.CharField(max_length=64, blank=True, default="")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="turn_usage",
    )
    """Who asked for the turn (`Turn.initiator_user`, else `enqueued_by`)."""

    at = models.DateTimeField()
    """When the turn finished (else when it was created) — the day it counts on."""

    model = models.CharField(max_length=100, blank=True, default="")
    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    cache_read_tokens = models.BigIntegerField(default=0)
    cache_creation_tokens = models.BigIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    runs = models.PositiveIntegerField(default=0)
    """How many `result` records were summed — a turn that failed over down the
    credential cascade runs `claude -p` more than once."""

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "at"]),
            models.Index(fields=["user", "at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"turn-usage:{self.turn_ref}"
