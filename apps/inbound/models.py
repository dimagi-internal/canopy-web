"""Inbound push: the doorbell for mail that should become a turn.

Framework tier. This app receives a Gmail Pub/Sub notification and rings the
runner that holds the mailbox credentials. It **never reads mail** — a Gmail
push carries ``{emailAddress, historyId}`` and no content, so something must
still call Gmail to learn what changed, and only the runner has the per-agent
``gog`` OAuth clients.

Two consequences of the doorbell shape, both load-bearing:

* No mail credential moves into the web app. The read path, the
  ``(thread, messageCount)`` idempotency key and the "agent's own reply" skip
  stay where they already work.
* **A forged doorbell cannot inject a fake email.** The worst a forged ping can
  do is cause a ``gog`` read that finds nothing. Verification still happens (a
  Google-signed OIDC JWT), but the blast radius is small by construction — a
  very different risk profile from an endpoint that accepted message bodies.
"""
from __future__ import annotations

from django.db import models


class InboundMailbox(models.Model):
    """A mailbox we accept push for, and the agent whose turn it becomes.

    Explicit data rather than convention. Deriving agent ``eva`` from
    ``eva@dimagi-ai.com`` by splitting on ``@`` works right up until a mailbox
    doesn't match its agent slug, and the failure is silent — a push arrives,
    resolves to nothing, and mail quietly keeps taking five minutes.
    """

    address = models.EmailField(unique=True)

    agent = models.ForeignKey(
        "agents.Agent",
        on_delete=models.CASCADE,
        related_name="inbound_mailboxes",
    )
    """The agent whose turn this mailbox's mail becomes. Its workspace is this
    row's tenant — there is no separate workspace FK to disagree with it."""

    enabled = models.BooleanField(default=True)
    """A switch, not a delete: turning push off for one mailbox keeps the row
    (and its watch state) so re-enabling doesn't mean re-provisioning."""

    last_push_at = models.DateTimeField(null=True, blank=True)
    """When we last received a verified push for this mailbox. Half of the
    push-miss check — the other half is the runner tagging how it discovered a
    message."""

    watch_expires_at = models.DateTimeField(null=True, blank=True)
    """When the Gmail ``users.watch`` registration lapses (Google caps it at 7
    days). Reported by the runner, which owns re-arming. NULL means *never
    registered*, which is why the push-miss check requires a live watch: with no
    watch there is nothing to have missed, and every poll-discovered message
    would otherwise be logged as a failure."""

    class Meta:
        ordering = ["address"]

    def __str__(self) -> str:  # pragma: no cover
        return f"inbound:{self.address}"

    @property
    def workspace_id(self) -> str:
        """This row's tenant, derived one hop away via the agent — the same shape
        `Turn` uses (it has no workspace FK of its own either)."""
        return self.agent.workspace_id
