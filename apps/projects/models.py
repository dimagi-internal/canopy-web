from django.conf import settings
from django.db import models


class Project(models.Model):
    VISIBILITY_CHOICES = [
        ("public", "Public"),
        ("private", "Private"),
    ]
    STATUS_CHOICES = [
        ("active", "Active"),
        ("stale", "Stale"),
        ("archived", "Archived"),
    ]

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    repo_url = models.URLField(blank=True, default="")
    deploy_url = models.URLField(blank=True, default="")
    visibility = models.CharField(max_length=10, choices=VISIBILITY_CHOICES, default="public")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="active")
    skills = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="projects_created",
        help_text="Who registered this project (null for pre-attribution rows).",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="projects",
        help_text="The tenant that owns this project. Nullable for migration "
        "safety; the API always assigns one (default workspace when unspecified).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name


class ProjectContext(models.Model):
    CONTEXT_TYPES = [
        ("current_work", "Current Work"),
        ("next_step", "Next Step"),
        ("summary", "Summary"),
        ("note", "Note"),
        ("insight", "Insight"),
        ("learning", "Learning"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="contexts")
    context_type = models.CharField(max_length=20, choices=CONTEXT_TYPES)
    # Which workflow on the project the entry belongs to ("" = the project as a
    # whole; "ddd" = its demo-driven-development loop). Lets one project feed
    # serve every kind of work done on the project without a file per workflow
    # in the repo (a DDD `learnings.md` collided at end-of-file across branches).
    scope = models.CharField(max_length=40, blank=True, default="", db_index=True)
    content = models.TextField()
    source = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project.slug}:{self.context_type}"


class ProjectAction(models.Model):
    STATUS_CHOICES = [
        ("started", "Started"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="actions")
    skill_name = models.CharField(max_length=100)
    session_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="started")
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["project", "skill_name", "-started_at"]),
        ]

    def __str__(self):
        return f"{self.project.slug}:{self.skill_name}:{self.status}"


class ProjectRun(models.Model):
    """One run of a kind of work ON a project — the generic record that a
    specific workflow specializes through ``kind`` and its ``state`` payload.

    DDD is the first kind: before this, a DDD run's state (iteration, findings,
    progress, gate decisions) lived only on the disk of the runner that started
    it, so no other machine could resume it, two machines could mint the same
    run_id, and the package page had to GUESS a run's phase from its latest
    review. Now the server mints the run_id, holds the state, and every runner
    reads and writes the same record.

    ``state`` is the workflow's own document (opaque here; versioned by
    ``state_version`` for optimistic concurrency). The flat columns
    (``status``/``phase``/``iteration``/``summary``) are what the product
    surfaces list and filter on without opening the blob.
    """

    RUNNING = "running"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="runs")
    kind = models.CharField(max_length=40, help_text='Workflow kind, e.g. "ddd".')
    # Globally unique: walkthroughs and reviews join to a run by this string.
    run_id = models.CharField(max_length=255, unique=True)
    subject = models.CharField(
        max_length=200, blank=True, default="",
        help_text="What the run is about within the kind (DDD: the narrative slug).",
    )
    title = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(
        max_length=40, default=RUNNING, db_index=True,
        help_text='"running" while live; else the kind\'s terminal status.',
    )
    phase = models.CharField(max_length=40, blank=True, default="")
    iteration = models.IntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True, help_text="Small kind-defined digest for lists.")
    state = models.JSONField(default=dict, blank=True, help_text="The kind's full run state document.")
    state_version = models.PositiveIntegerField(default=0)
    holder = models.CharField(
        max_length=200, blank=True, default="",
        help_text="The runner/host that last wrote the state.",
    )
    holder_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="project_runs_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["project", "kind", "-updated_at"]),
            models.Index(fields=["project", "kind", "subject"]),
        ]

    def __str__(self) -> str:
        return f"{self.project.slug}:{self.kind}:{self.run_id}"
