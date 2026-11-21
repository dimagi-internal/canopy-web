"""A STORED agent's definition: its files, kept by canopy-web in revisions.

A repo agent's definition is its git repository (`apps/agents/definition.py`). A stored
agent (`Agent.source == "stored"`) has none: its definition is a file tree here, in
append-only revisions. The policy over what a tree may hold, and the manifest root that
names one, are `canopy_sdk.agent_definition`; the services are `apps/agent_definitions`.
Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AgentDefinitionBlob(models.Model):
    """One file's content, by its sha256. Shared by every revision (of any agent) that
    holds those exact bytes, and never changed: a new content is a new blob."""

    sha256 = models.CharField(max_length=64, primary_key=True)
    size = models.PositiveIntegerField()
    #: The bytes: in the attachment bucket, or `local` on a box with none (a dev laptop).
    storage_key = models.CharField(max_length=500, blank=True, default="")
    local = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"blob:{self.sha256[:12]}"


class AgentDefinitionRevision(models.Model):
    """One state of a stored agent's file tree.

    `tree` is `{path: {"sha256", "size", "executable"}}`; `root_sha256` is its
    `manifest_root`. Rows are never deleted and a tree is never edited once published:
    a draft becomes `published` (and the old live one `superseded`) or `discarded`;
    a rollback is a NEW revision copying an old tree. At most one draft per agent.
    """

    DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = "draft", "published", "superseded", "discarded"
    STATUS_CHOICES = [(DRAFT, "draft"), (PUBLISHED, "published"), (SUPERSEDED, "superseded"),
                      (DISCARDED, "discarded")]

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="definition_revisions")
    number = models.PositiveIntegerField()
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    tree = models.JSONField(default=dict)
    root_sha256 = models.CharField(max_length=64)
    message = models.TextField(blank=True, default="")
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    #: The turn an agent's own edit came from: set exactly when the agent (not a person)
    #: wrote this draft, which is also when an approval was asked for.
    author_agent_turn = models.ForeignKey("harness.Turn", null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="+")
    #: True when an agent (its own login, a turn's token) wrote it — kept apart from the
    #: turn, which may be unknown or deleted.
    by_agent = models.BooleanField(default=False)
    approval = models.ForeignKey("approvals.ApprovalRequest", null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DRAFT)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["agent", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "number"], name="uniq_agent_definition_revision"),
            models.UniqueConstraint(fields=["agent"], condition=models.Q(status="draft"),
                                    name="one_agent_definition_draft"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"definition:{self.agent_id}#{self.number}({self.status})"
