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


class InboundPushConfig(models.Model):
    """One workspace's push setup — the thing that makes this multi-tenant.

    Verification used to be two deployment-global settings, which meant exactly
    ONE Workspace could ever be verified: a second tenant's subscription signs
    with its own service account, and a single pinned signer rejects it. A LIST
    of allowed signers is not the fix either — that would let tenant A's service
    account push for tenant B's mailbox. Verification has to bind to the tenant.

    So it binds here, and the tenant is named in the URL
    (``/api/inbound/gmail/{workspace}/``) rather than parsed out of the payload.
    That ordering matters: the endpoint verifies BEFORE decoding anything, so no
    attacker-controlled byte gets to choose which credential it is checked
    against. It also means an unknown mailbox can be logged against the right
    workspace instead of leaking the address into the default one.
    """

    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="inbound_push_config",
        primary_key=True,
    )

    audience = models.CharField(max_length=500, blank=True, default="")
    """The OIDC audience the Pub/Sub subscription was created with —
    conventionally the push endpoint URL. Blank REFUSES every push for this
    workspace: an unconfigured tenant must not quietly accept anonymous callers,
    and refusing costs latency (the 300s poll still runs), never mail."""

    service_account = models.CharField(max_length=320, blank=True, default="")
    """The service account the subscription signs with. Audience alone is not
    identity — anyone who learns the audience string could mint a token for it
    from a different account — so this pins the signer. Blank means audience-only
    verification, which is weaker; the UI says so."""

    watch_topic = models.CharField(max_length=500, blank=True, default="")
    """``projects/<p>/topics/<t>``. Served to the runner so a tenant configures
    its topic HERE rather than by hand-editing runner.json on every box. Blank
    means this workspace's mailboxes are never armed."""

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"inbound-config:{self.workspace_id}"

    @property
    def verifies(self) -> bool:
        """Whether this workspace can accept a push at all."""
        return bool(self.audience)


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
