"""The DB adapter's tables — the unified agent run-lifecycle in Postgres.

These rows are the *DB-as-truth* backing for the storage-agnostic read model in
`schemas.py` (Run / Step / Artifact / Verdict / Decision / Gate). A canopy-hosted
agent's runs live here; ACE's runs live in Drive and are read by the (separate)
Drive adapter into the *same* read model — see the design spec §3/§4.

This app is FRAMEWORK tier: it may FK to `agents.Agent` (framework) but must not
import any product app. See ARCHITECTURE.md.
"""
from __future__ import annotations

from django.db import models

from apps.agents.models import Agent


class AgentRun(models.Model):
    """One execution of an agent's lifecycle — a sequence of steps.

    `status` is NOT a column here: the read model derives run status from the
    steps map (all terminal → complete; else in_progress). We persist
    `completed_at` so the DB adapter can answer "done" cheaply, but the canonical
    derived status is computed in the read model.
    """

    REVIEW, AUTO = "review", "auto"
    MODE_CHOICES = [(REVIEW, "Review"), (AUTO, "Auto")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="runs")
    label = models.CharField(max_length=300, blank=True, default="", help_text="Human label for the run.")
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=REVIEW)
    # Stored convenience mirror of the derived status; the read model recomputes.
    status = models.CharField(max_length=20, blank=True, default="", help_text="Mirror of derived status; read model recomputes from steps.")
    current_step = models.CharField(max_length=120, blank=True, default="", help_text="Key of the step currently in focus.")
    forked_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="forks",
        help_text="The run this was forked from, if any.",
    )
    session_link = models.URLField(max_length=500, blank=True, default="", help_text="Link to the originating Claude/agent session.")

    # ---- run documents: a run of a KIND of work on an agent's PROJECT ----------
    # The generic record a specific workflow specializes. DDD is the first kind:
    # its state (iteration, findings, progress, gate decisions) used to live only
    # on the disk of the runner that started it, so no other machine could resume
    # it and two machines could mint the same run id. All optional — a plain
    # lifecycle run (ACE's) leaves them empty. See apps/agent_runs/documents.py.
    project = models.ForeignKey(
        "agents.AgentProject", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="runs", help_text="The agent project this run is work on.",
    )
    kind = models.CharField(max_length=40, blank=True, default="", db_index=True, help_text='e.g. "ddd".')
    ext_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True,
        help_text="The workflow's own run id (DDD: <narrative>-YYYY-MM-DD-NNN). Globally unique: "
        "walkthroughs and reviews join to the run by this string.",
    )
    subject = models.CharField(max_length=200, blank=True, default="", help_text="DDD: the narrative slug.")
    summary = models.JSONField(default=dict, blank=True, help_text="Small kind-defined digest for lists.")
    state = models.JSONField(default=dict, blank=True, help_text="The kind's full run-state document.")
    state_version = models.PositiveIntegerField(default=0)
    holder = models.CharField(max_length=200, blank=True, default="", help_text="Runner (user@host) that last wrote.")
    holder_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["agent", "-created_at"]),
            models.Index(fields=["forked_from"]),
            models.Index(fields=["kind", "subject"]),
        ]

    def __str__(self) -> str:
        return f"run:{self.agent.slug}:{self.pk}"


class AgentRunStep(models.Model):
    """One ordered step/phase of a run. Status is a real column here (unlike the
    run's derived status) — a step is the atomic unit the lifecycle advances."""

    PENDING, RUNNING, COMPLETE, FAILED, SKIPPED = (
        "pending", "running", "complete", "failed", "skipped")
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (RUNNING, "Running"),
        (COMPLETE, "Complete"),
        (FAILED, "Failed"),
        (SKIPPED, "Skipped"),
    ]
    TERMINAL = {COMPLETE, SKIPPED}

    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    key = models.CharField(max_length=120, help_text="Stable step key, e.g. the phase/skill slug.")
    ordinal = models.IntegerField(default=0, help_text="Order within the run.")
    title = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["run", "ordinal", "id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "key"], name="uniq_run_step_key"),
        ]
        indexes = [models.Index(fields=["run", "ordinal"])]

    def __str__(self) -> str:
        return f"step:{self.run_id}:{self.key}:{self.status}"


class AgentRunArtifact(models.Model):
    """A many-per-step artifact, attributed to the producing step/skill (the
    `role`). Mirrors ACE's manifest-based artifact attribution."""

    step = models.ForeignKey(AgentRunStep, on_delete=models.CASCADE, related_name="artifacts")
    name = models.CharField(max_length=300)
    url = models.URLField(max_length=1000, blank=True, default="", help_text="Link/ref to the artifact (Drive url, gs://, etc.).")
    mime_type = models.CharField(max_length=120, blank=True, default="")
    size = models.BigIntegerField(null=True, blank=True, help_text="Bytes, if known.")
    role = models.CharField(max_length=120, blank=True, default="", help_text="Producer skill/tag.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["step", "name", "id"]
        indexes = [models.Index(fields=["step"])]

    def __str__(self) -> str:
        return f"artifact:{self.step_id}:{self.name}"


class AgentRunVerdict(models.Model):
    """A verdict attached to a step. `kind=qa` is binary and gates `kind=judge`
    (QA fail → judge incomplete). Verdicts are asynchronous to steps."""

    JUDGE, QA = "judge", "qa"
    KIND_CHOICES = [(JUDGE, "Judge"), (QA, "QA")]

    step = models.ForeignKey(AgentRunStep, on_delete=models.CASCADE, related_name="verdicts")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    score = models.FloatField(null=True, blank=True)
    passed = models.BooleanField(null=True, blank=True)
    criteria = models.JSONField(default=dict, blank=True, help_text="Per-dimension scores / rubric breakdown.")
    rationale = models.TextField(blank=True, default="")
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["step", "kind", "id"]
        indexes = [models.Index(fields=["step", "kind"])]

    def __str__(self) -> str:
        return f"verdict:{self.step_id}:{self.kind}"


class AgentRunDecision(models.Model):
    """An entry in the auditable decisions log (ACE Phase 4). The AI proposes a
    default; a human may override it."""

    AI_DEFAULT, OVERRIDDEN = "ai-default", "overridden"
    STATUS_CHOICES = [(AI_DEFAULT, "AI default"), (OVERRIDDEN, "Overridden")]

    step = models.ForeignKey(AgentRunStep, on_delete=models.CASCADE, related_name="decisions")
    question = models.TextField(help_text="What was being decided.")
    ai_default = models.TextField(blank=True, default="", help_text="The AI's proposed answer.")
    override = models.TextField(blank=True, default="", help_text="The human override, if any.")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=AI_DEFAULT)
    reasoning = models.TextField(blank=True, default="")
    evidence_basis = models.TextField(blank=True, default="", help_text="What the decision was grounded in.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["step", "id"]
        indexes = [models.Index(fields=["step"])]

    def __str__(self) -> str:
        return f"decision:{self.step_id}:{self.status}"


class AgentRunGate(models.Model):
    """A pause point on a step — the run-lifecycle analogue of a board command.
    Recording a gate decision is how a human hands the ball back to the agent."""

    step = models.ForeignKey(AgentRunStep, on_delete=models.CASCADE, related_name="gates")
    decision = models.CharField(max_length=120, blank=True, default="", help_text="The decision recorded at the gate.")
    decided_by = models.CharField(max_length=200, blank=True, default="", help_text="Who decided (email).")
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["step", "id"]
        indexes = [models.Index(fields=["step"])]

    def __str__(self) -> str:
        state = "decided" if self.decided_at else "open"
        return f"gate:{self.step_id}:{state}"
