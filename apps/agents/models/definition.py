"""A STORED agent's definition: a git repository canopy-web hosts, plus its revisions.

A repo agent's definition is its GitHub repository (`apps/agents/definition.py`). A
stored agent's (`Agent.source == "stored"`) is a repository canopy-web keeps in the
database (`AgentGitObject`, `AgentGitRef`) and serves read-only over git's smart HTTP;
each `AgentDefinitionRevision` is one commit in it with the approval layer on top. The
policy over what a tree may hold, and the manifest root that names one, are
`canopy_sdk.agent_definition`; the services are `apps/agent_definitions`.
Doc: `docs/architecture/stored-agents.md`.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class AgentGitObject(models.Model):
    """One git object (blob, tree, commit or tag) of a stored agent's repository.

    Each stored agent's definition is a bare git repository canopy-web keeps in these
    rows and `AgentGitRef`, never on disk (`apps/agents/definition_git.py`). Objects
    are scoped to their agent, so who may read a repository is a question about one
    agent; `data` is the object's raw content, zlib-compressed; `sha` is git's SHA-1
    of it, so the repository is byte-compatible with any git host."""

    BLOB, TREE, COMMIT, TAG = 3, 2, 1, 4
    TYPE_CHOICES = [(COMMIT, "commit"), (TREE, "tree"), (BLOB, "blob"), (TAG, "tag")]

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="git_objects")
    sha = models.CharField(max_length=40)
    type = models.PositiveSmallIntegerField(choices=TYPE_CHOICES)
    data = models.BinaryField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "sha"], name="uniq_agent_git_object")]

    def __str__(self) -> str:  # pragma: no cover
        return f"git:{self.agent_id}:{self.sha[:12]}"


class AgentGitRef(models.Model):
    """One ref of a stored agent's repository: `refs/heads/main` (live),
    `refs/heads/draft`, `refs/tags/r<N>` and `refs/heads/imported`."""

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="git_refs")
    name = models.CharField(max_length=200)
    sha = models.CharField(max_length=40)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_git_ref")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.name}={self.sha[:12]}"


class AgentDefinitionRevision(models.Model):
    """One state of a stored agent's file tree: a commit in its repository.

    `commit` is the git commit; `tree` (`{path: {"sha256", "size", "executable"}}`) and
    `root_sha256` (its `manifest_root`) are read from that commit's tree when it is
    written, and kept so the API answers without reading every file. Rows are never
    deleted and a tree is never edited once published: a draft becomes `published` (and
    the old live one `superseded`) or `discarded`; a rollback is a NEW revision copying
    an old tree. At most one draft per agent.
    """

    DRAFT, PUBLISHED, SUPERSEDED, DISCARDED = "draft", "published", "superseded", "discarded"
    STATUS_CHOICES = [(DRAFT, "draft"), (PUBLISHED, "published"), (SUPERSEDED, "superseded"),
                      (DISCARDED, "discarded")]

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="definition_revisions")
    number = models.PositiveIntegerField()
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    tree = models.JSONField(default=dict)
    root_sha256 = models.CharField(max_length=64)
    #: The revision's commit (SHA-1 hex). A draft's is the commit on `refs/heads/draft`;
    #: a published revision's is the commit `refs/tags/r<number>` names.
    commit = models.CharField(max_length=40, blank=True, default="")
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
    #: The agent that did this revision's person's act (create, convert, publish, roll back)
    #: AS that person, under the act-as-you permission `act_as_grant` they approved with
    #: their passkey (`harness.acting_person.person_or_act_as`). `author_user` /
    #: `published_by` stay the person. Both are empty when the person acted themselves.
    acting_agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    act_as_grant = models.ForeignKey("harness.ActAsGrant", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    #: The agent whose own login published this revision (or created it live, rolling back)
    #: as ITSELF, because it administers this revision's agent and is not that agent
    #: (`harness.acting_person.admin_agent`). `published_by` is then that agent's login.
    #: Empty when a person published it, themselves or through an act-as-you permission.
    published_by_agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name="+")
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
