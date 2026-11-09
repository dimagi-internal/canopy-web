"""Agent models: kb."""

from django.db import models
from pgvector.django import HalfVectorField

from .agent import Agent
from .work import AgentProject


class KbPassage(models.Model):
    """One passage of a workspace project's published knowledge base with its vector:
    where it is, what the vector was made from (`sha256` of
    `canopy_kb.vectors.embedding_text`) and the vector. DORMANT: nothing writes or
    reads these while KB_VECTORS is off (`kb_vectors.py`).

    The passage text stays in the KB file (`kb_store`); a search maps a nearest
    (item_id, ord) back to its row there. Rows are written only by
    `kb_vectors.sync`, which diffs the file by `sha256` and embeds only what is
    new or changed. A row whose `embedding_model` is not the current one counts
    as missing. On PostgreSQL an HNSW index (cosine) serves the nearest-neighbour
    query, filtered by project and type with pgvector's iterative scan
    (migration 0064); there is no vector search on SQLite."""

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_passages")
    item_id = models.CharField(max_length=300)
    ord = models.IntegerField()
    sha256 = models.CharField(max_length=64)
    item_type = models.CharField(max_length=64, blank=True, default="")
    #: As the KB has it: "YYYY-MM-DD", or "" when the item has no date.
    date = models.CharField(max_length=32, blank=True, default="")
    title = models.TextField(blank=True, default="")
    url = models.URLField(max_length=1000, blank=True, default="")
    embedding = HalfVectorField(dimensions=3072)
    embedding_model = models.CharField(max_length=100)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "item_id", "ord"], name="kb_passage_unique"),
        ]
        indexes = [models.Index(fields=["project", "item_type"], name="kb_passage_type")]

    def __str__(self):
        return f"kb:{self.project_id}:{self.item_id}#{self.ord}"


class SkillHistorySync(models.Model):
    """The last pull of this agent's skill history from its repo.

    A CACHE of the repo, replaced wholesale on each sync — the same stance as
    `AgentSkill`. `groups` / `checks` / `present` are a HEAD-only snapshot read
    whole and never queried by field, so they are JSON rather than tables.

    `synced_with` is the GitHub login whose grant produced this history — the
    agent owner's, never the viewer's (see the spec's "Whose credential").
    """

    agent = models.OneToOneField(Agent, on_delete=models.CASCADE, related_name="skill_history_sync")
    # 64: a SHA-256 repository's object ids are 64 hex characters.
    head_sha = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)
    synced_with = models.CharField(max_length=100, blank=True, default="")
    last_error = models.TextField(blank=True, default="")
    # One of skill_history.CREDENTIAL_STATES — what the last attempt found.
    last_state = models.CharField(max_length=32, blank=True, default="")
    sync_started_at = models.DateTimeField(null=True, blank=True)
    # Set on every attempt, success or failure. `synced_at` moves only on
    # success, so without this a failing agent (no grant, repo not granted)
    # would refresh the owner's token and clone on every page load.
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    groups = models.JSONField(default=list, blank=True)
    checks = models.JSONField(default=dict, blank=True)
    present = models.JSONField(default=list, blank=True)

    def __str__(self):
        return f"skill-history:{self.agent.slug}@{self.head_sha[:8]}"


class SkillHistoryCommit(models.Model):
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="skill_history_commits")
    sha = models.CharField(max_length=64)
    committed_at = models.DateTimeField()
    subject = models.TextField()
    body = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["committed_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "sha"], name="uniq_agent_skill_history_sha"),
        ]


class SkillRevision(models.Model):
    commit = models.ForeignKey(SkillHistoryCommit, on_delete=models.CASCADE, related_name="revisions")
    skill = models.CharField(max_length=120)
    lines_after = models.IntegerField()
    added = models.IntegerField()
    deleted = models.IntegerField()

    class Meta:
        indexes = [models.Index(fields=["skill"])]


class KbVersion(models.Model):
    """One published state of a workspace project's knowledge base: the version log.

    A FULL publish is a whole kb.sqlite its steward built; canopy-web never builds one
    (the DELTA kind, which canopy-web once built in its web process, is kept only in
    the log of earlier versions). Each carries the curation state it was built from
    (`curation_sha`), which the next refresh starts from. A ROLLBACK points the project
    back at an earlier version's file and state. What changed is the file's own history
    (`whats_new`); `added`, `changed` and `removed` count it.
    """

    FULL, DELTA, ROLLBACK = "full", "delta", "rollback"
    KIND_CHOICES = [(FULL, "Full file"), (DELTA, "Delta"), (ROLLBACK, "Rollback")]
    PENDING, BUILDING, READY, FAILED = "pending", "building", "ready", "failed"
    STATUS_CHOICES = [(PENDING, "Pending"), (BUILDING, "Building"), (READY, "Ready"), (FAILED, "Failed")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_versions")
    number = models.PositiveIntegerField()
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    #: The version this one was made from (a delta's base, a rollback's previous head).
    base = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: A rollback's target.
    rollback_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    #: The curation state it was built from: a root in the steward's results store
    #: (`canopy_kb.curate.State`). Blank for a file published without one (`canopy kb push`).
    curation_sha = models.CharField(max_length=64, blank=True, default="")
    #: Its kb.sqlite.gz. Cleared when pruned (`kb_versions.KEEP_FILES`): an older
    #: version stays in the log but can no longer be rolled back to.
    kb_key = models.CharField(max_length=300, blank=True, default="")
    kb_sha256 = models.CharField(max_length=64, blank=True, default="")
    kb_bytes = models.BigIntegerField(null=True, blank=True)
    added = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    removed = models.PositiveIntegerField(default=0)
    #: What the publisher sent for this version (records and parts), in bytes.
    uploaded_bytes = models.BigIntegerField(default=0)
    #: The built file's summary (`canopy_kb` summary: items, passages, …).
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    #: Who published it: the agent whose login sent it, else blank.
    published_by = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: The Drive replica's outcome when the project keeps one ("" when it does not).
    replica = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        db_table = "agents_kb_version"
        ordering = ["project", "-number"]
        constraints = [models.UniqueConstraint(fields=["project", "number"], name="kb_version_number_unique")]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id} v{self.number} {self.kind} {self.status}"


class KbSource(models.Model):
    """Where a knowledge base's documents come from. Every document records its source
    (`meta.source` on its record); what happens after the source is shared by all.

    * `web` — a curator's crawler (GiveWell's, in eva). `config` notes what it reads.
      The only kind.
    """

    WEB = "web"
    KIND_CHOICES = [(WEB, "Web crawl")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="kb_sources")
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    name = models.CharField(max_length=100)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agents_kb_source"
        ordering = ["project", "id"]
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="kb_source_name_unique")]
