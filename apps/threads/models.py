"""Agent threads — a bounded, moderated conversation between named fleet agents.

Jonathan, 2026-10-07: agents should talk to each other directly, as "the start of
a robust approach" to increasingly complicated agent→agent interactions. The first
use is a huddle's agreement step: when a teammate is in on an idea only with
changes, the idea's author and that teammate settle it in a direct thread.

What is STORED here is only the thread's frame and its limits — who is in it, what
it is for, the opening material, how many messages it may use and until when, and
how it ended. Each MESSAGE is an ordinary harness turn for the speaking agent,
tagged ``origin_ref = {"kind": "thread_message", "thread": <id>, "n": <1-based>,
"speaker": <slug>}``; its text is DERIVED from that turn's reply block (services.py),
exactly the way apps/huddles reads a round reply. The limits are ENFORCED where the
turn is created (guard.py, called from ``harness.services.enqueue_turn``), so they
hold whoever dispatches — not by trusting the moderator to count.

No agent waits on another inside its own turn: the MODERATOR (canopy's
``canopy thread run``, run by whoever opened the thread) sends the next message and
decides when it ends, then closes the thread here with its outcome.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

#: How long a thread may run when the opener does not say.
DEFAULT_DEADLINE = dt.timedelta(minutes=90)
DEFAULT_MAX_MESSAGES = 4
MAX_MESSAGES_CAP = 12
MIN_PARTICIPANTS, MAX_PARTICIPANTS = 2, 6


def new_thread_id() -> str:
    return f"thr-{uuid.uuid4().hex[:12]}"


class AgentThread(models.Model):
    OPEN = "open"
    SETTLED = "settled"
    OUT_OF_BUDGET = "out_of_budget"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (OPEN, "Open"),
        (SETTLED, "Settled"),
        (OUT_OF_BUDGET, "Out of budget"),
        (TIMED_OUT, "Timed out"),
        (CANCELLED, "Cancelled"),
    ]
    CLOSED_STATUSES = (SETTLED, OUT_OF_BUDGET, TIMED_OUT, CANCELLED)

    id = models.CharField(primary_key=True, max_length=20, default=new_thread_id, editable=False)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="agent_threads",
    )
    """The opener's workspace — the moderator agent's. Writes (open, close) need
    `agent.work` here; reads need membership."""

    kind = models.CharField(max_length=40)
    """What the conversation is for, as a type ("agreement"). Free text: the moderator
    decides what a kind means; canopy-web only stores and enforces."""

    purpose = models.CharField(max_length=300)
    participants = models.JSONField(default=list)
    """[{"agent": <slug>, "role": <free text>}], 2..6 distinct agents."""

    moderator = models.CharField(max_length=80)
    parent = models.JSONField(default=dict, blank=True)
    """What the thread hangs off, e.g. {"huddle": <id>, "title": <idea>, "lead": <slug>}."""

    context = models.TextField(blank=True, default="")
    """The opening material every message prompt quotes verbatim."""

    max_messages = models.PositiveSmallIntegerField(default=DEFAULT_MAX_MESSAGES)
    deadline_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=OPEN)
    outcome = models.JSONField(default=dict, blank=True)
    """Set on close. For kind=agreement: {"result": "agreed"|"not_agreed",
    "proposal": {...}, "why": "..."}."""

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agent_threads",
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "status", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.id} ({self.kind}, {self.status})"

    @property
    def agents(self) -> list[str]:
        return [str(p.get("agent") or "") for p in (self.participants or []) if isinstance(p, dict)]
