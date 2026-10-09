"""A built plugin archive: one agent repo at one commit, as the zip Claude Code installs.

canopy-web serves each workspace's agent plugins as a Claude Code marketplace
(canopy-web#1376) so a member with no GitHub account can install them. GitHub
stays the source of truth: an archive is BUILT from the repo at a merged commit
and never edited here, so a row is immutable — a new merge is a new row.

Keyed by (repo, commit) rather than by agent, because the definition is the
repo, not the agent row (`apps/agents/definition.py`): two instances pointing
at the same repo serve the same bytes.

The bytes live in the row (Postgres `bytea`), like `harness.TurnTranscript`.
Plugins are small (a few MB of markdown and scripts) and the stored bytes ARE
the `sha256` the marketplace pins — rebuilding from GitHub on each download
would not be byte-stable (GitHub does not promise a stable zipball), and a
changed digest makes Claude Code refuse the install.
"""
from __future__ import annotations

from django.db import models


class PluginArchive(models.Model):
    #: `owner/repo` on github.com.
    repo = models.CharField(max_length=200)
    commit_sha = models.CharField(max_length=40)
    #: Where the plugin root sits inside the repo ("" = the repo root).
    subdir = models.CharField(max_length=300, blank=True, default="")
    #: From the archive's own `.claude-plugin/plugin.json` — what users type
    #: before `@` and what namespaces the plugin's skills.
    plugin_name = models.CharField(max_length=128)
    #: The version written INTO the served plugin.json: the repo's own version
    #: plus `+<short sha>`, so every merge is a version change Claude Code sees.
    version = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    content = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["repo", "commit_sha"], name="plugin_archive_repo_commit"),
        ]
        indexes = [models.Index(fields=["repo", "-created_at"], name="plugin_archive_latest")]

    def __str__(self) -> str:
        return f"{self.repo}@{self.short_sha} ({self.plugin_name} {self.version})"

    @property
    def short_sha(self) -> str:
        return self.commit_sha[:12]
