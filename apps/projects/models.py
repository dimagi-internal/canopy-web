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
    default_identity_agent = models.ForeignKey(
        "agents.Agent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="default_identity_projects",
        help_text="Fallback GitHub identity for a project turn with no agent of "
        "its own (apps.agents.delegations.turn_agent) — lends this agent's "
        "owner's delegated token and git identity. Unset keeps the turn's "
        "existing refusal: no shared fallback.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name


#: The context type of the retired Insights feed (2026-10). The choice stays so
#: the leftover rows still validate; nothing writes or returns them.
RETIRED_CONTEXT_TYPE = "insight"


class ProjectContext(models.Model):
    CONTEXT_TYPES = [
        ("current_work", "Current Work"),
        ("next_step", "Next Step"),
        ("summary", "Summary"),
        ("note", "Note"),
        ("insight", "Insight"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="contexts")
    context_type = models.CharField(max_length=20, choices=CONTEXT_TYPES)
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
