"""Agent tasks: a caller hands an agent a task and structured input, and gets a
schema-validated result back — not a chat, not a transcript.

FRAMEWORK tier (L1 core: a turn's structured answer). One row per task. Creating
one enqueues ONE ordinary harness turn for the agent (`origin_ref.agent_task`);
the agent reads the task with `get_agent_task` and answers with
`submit_agent_task_result`. **The task is done only when a result that
validates against `output_schema` is submitted** — never because a turn ended.
A turn that ends without one is nudged (`signals.py`), and a task past its
deadline fails (`services.expire`). docs/architecture/agent-tasks.md.

Not `agents.AgentTask` (a board task: a person's or agent's ask, with a key and
a status category). Tenancy is DERIVED from the agent (`agent.workspace`), as an
agent turn's is: a second copy would drift, and a moved agent takes its tasks.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class AgentTask(models.Model):
    QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
    STATUS_CHOICES = [(QUEUED, "Queued"), (RUNNING, "Running"), (DONE, "Done"),
                      (FAILED, "Failed"), (CANCELLED, "Cancelled")]
    #: Still waiting for its result: the only states a result is accepted in.
    OPEN = (QUEUED, RUNNING)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="structured_tasks")
    #: What kind of task, e.g. `gmail-triage.review`. When it names one of the
    #: agent's skills (whole, or before the first dot) the turn is told to use it.
    name = models.CharField(max_length=120)
    instructions = models.TextField(blank=True, default="")
    input = models.JSONField(default=dict, blank=True)
    #: A JSON Schema, checked to BE one at create; the result must validate against it.
    output_schema = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    #: Follow-up turns sent because a turn ended without a result (at most MAX_NUDGES).
    nudges = models.PositiveSmallIntegerField(default=0)
    #: The latest turn working it. Only THIS turn's end can nudge or fail it.
    turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="agent_tasks")
    #: How every turn for it is routed (the create's `routing`).
    routing = models.CharField(max_length=15, default="prefer_local")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name="agent_tasks_created")
    timeout_s = models.PositiveIntegerField(default=900)
    #: created_at + timeout_s; an open task past it fails.
    deadline_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "deadline_at"])]

    def __str__(self) -> str:
        return f"{self.name} for {self.agent_id} ({self.status})"
