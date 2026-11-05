"""One JSON document an agent keeps on canopy-web, so its own state survives a box change."""
from __future__ import annotations

from django.db import models


class AgentStateDocument(models.Model):
    """The backup of what an agent keeps on its box. The box's copy is the working one.

    canopy-web stores it and never reads inside it: it is not an input to anything. Who
    may read and write it: `apps/harness/agent_state.py`. Tenancy comes from the agent,
    whose workspace is NOT NULL; deleting the agent deletes this."""

    agent = models.OneToOneField("agents.Agent", on_delete=models.CASCADE, related_name="state_document")
    document = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    """Bumped by every write; a write names the version it read (`agent_state.write`)."""
    updated_by_turn = models.UUIDField(null=True, blank=True)
    """The running turn that wrote it. An id rather than an FK: this outlives turn retention."""
    updated_at = models.DateTimeField(auto_now=True)
