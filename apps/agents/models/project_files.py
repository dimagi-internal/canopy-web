"""A project's files: a git repository canopy-web hosts for one project, plus its revisions.

A stored agent's definition is a repository canopy keeps in the database
(`definition.py`); a project gets the same kind of repository for the tooling it shares
with whoever works on it — its skills, the scripts they run and their docs. Objects and
refs are scoped to the project, written by `apps/agents/definition_git.py` (with
`owner="project_id"`); each `ProjectFilesRevision` is one commit with who wrote and who
published it. The services and the file policy are `apps/agents/project_files.py`.
Doc: `docs/architecture/project-files.md`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class ProjectGitObject(models.Model):
    """One git object (blob, tree, commit or tag) of a project's repository: `data` is its
    raw content, zlib-compressed; `sha` is git's SHA-1 of it."""

    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, related_name="git_objects")
    sha = models.CharField(max_length=40)
    type = models.PositiveSmallIntegerField()
    data = models.BinaryField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "sha"], name="uniq_project_git_object")]

    def __str__(self) -> str:  # pragma: no cover
        return f"git:project{self.project_id}:{self.sha[:12]}"


class ProjectGitRef(models.Model):
    """One ref of a project's repository: `refs/heads/main` (live), `refs/heads/draft` and
    `refs/tags/r<N>`."""

    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, related_name="git_refs")
    name = models.CharField(max_length=200)
    sha = models.CharField(max_length=40)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="uniq_project_git_ref")]

    def __str__(self) -> str:  # pragma: no cover
        return f"project{self.project_id}:{self.name}={self.sha[:12]}"


class ProjectFilesRevision(models.Model):
    """One state of a project's files: a commit in its repository.

    `tree` (`{path: {"sha256", "size", "executable"}}`) and `root_sha256` are read from
    the commit when it is written, so the API answers without reading every file. Rows are
    never deleted: a draft becomes `published` (and the old live one `superseded`) or
    `discarded`; a rollback is a NEW revision copying an old tree. At most one draft and
    one published revision per project.
    """

    DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = "draft", "published", "superseded", "discarded"
    STATUS_CHOICES = [(DRAFT, "draft"), (PUBLISHED, "published"), (SUPERSEDED, "superseded"),
                      (DISCARDED, "discarded")]

    project = models.ForeignKey("agents.AgentProject", on_delete=models.CASCADE, related_name="file_revisions")
    number = models.PositiveIntegerField()
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    tree = models.JSONField(default=dict)
    root_sha256 = models.CharField(max_length=64)
    commit = models.CharField(max_length=40)
    message = models.TextField(blank=True, default="")
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    #: The agent whose login wrote this draft; empty when a person wrote it.
    author_agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["project", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["project", "number"], name="uniq_project_files_revision"),
            models.UniqueConstraint(fields=["project"], condition=models.Q(status="draft"),
                                    name="one_project_files_draft"),
            models.UniqueConstraint(fields=["project"], condition=models.Q(status="published"),
                                    name="one_project_files_live"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"files:project{self.project_id}#{self.number}({self.status})"
