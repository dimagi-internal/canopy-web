"""A dynamic view: a screen whose code is data, drawn in a sandbox over canopy's own reads.

A view belongs to a workspace (NOT NULL — a row with no workspace is visible to
nobody) and, optionally, to one of that workspace's projects. Its `code` is JSX
that defines `function View(props)`; its `sources` are the catalog reads
(`catalog.py`) the canopy page makes FOR it, as the viewer, before it draws.
Design: docs/architecture/dynamic-views.md.

Every save is a new `DynamicViewRevision`, append-only, so any version can be
read back or restored (`services.rollback`).
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class DynamicView(models.Model):
    SLUG_RE = r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$"

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="dynamic_views")
    #: The project the view is about, or none for a workspace-wide view. Always a
    #: project OF `workspace` (`services.save` refuses any other).
    project = models.ForeignKey("agents.AgentProject", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="dynamic_views")
    slug = models.CharField(max_length=60)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    #: JSX defining `function View(props)`; compiled and run only inside the frame.
    code = models.TextField()
    #: Declared catalog reads: [{"alias", "model", "params"?, "path"?}].
    sources = models.JSONField(default=list, blank=True)
    #: The agent tasks the code may start, as the viewer: [{"agent", "name"}]
    #: (`declared_tasks.py`). Empty for a view that only reads.
    agent_tasks = models.JSONField(default=list, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="dynamic_views_created")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="dynamic_views_updated")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title", "id"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "slug"], name="dynamic_view_slug_per_workspace"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.slug}"


class DynamicViewRevision(models.Model):
    """One saved version of a view, kept whole so it can be restored."""

    view = models.ForeignKey(DynamicView, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    code = models.TextField()
    sources = models.JSONField(default=list, blank=True)
    agent_tasks = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=300, blank=True, default="")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["view", "version"], name="dynamic_view_revision_version"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.view_id}@{self.version}"
