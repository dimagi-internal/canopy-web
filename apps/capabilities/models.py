"""Cloud capabilities: machines an agent leases for as long as it needs them.

FRAMEWORK tier. canopy-web owns the generic half — the manifest an agent
publishes, the workspace admin's grant, and the lease lifecycle over AWS — and
nothing domain-specific: the stack, the image and the CLI on it are the agent's
(docs/architecture/capabilities.md).

Three rows:

- ``AgentCapability``: what the agent DECLARED (its ``capability.yaml``, published
  by ``canopy-runtime publish-capabilities``), plus the stack Outputs canopy-web
  resolved when it was published. Never deleted: a capability left out of a
  publish is RETIRED, so its leases keep their history.
- ``CapabilityGrant``: a workspace admin ALLOWING the agent to use it, optionally
  lowering its limits. No grant, no lease.
- ``CapabilityLease``: one machine for one use. Its id is the EC2 ClientToken,
  so a retried launch can never start a second instance.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class AgentCapability(models.Model):
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="capabilities")
    name = models.CharField(max_length=40)
    kind = models.CharField(max_length=32)
    #: The validated manifest (canopy_runtime.schema.CapabilityManifest, as JSON).
    manifest = models.JSONField(default=dict)
    region = models.CharField(max_length=32)
    stack = models.CharField(max_length=128)
    #: The stack's Outputs at publish time (DescribeStacks): LaunchTemplateId,
    #: ArtifactsBucketName, InstanceRoleArn, InstanceRoleId, Capability, Owner.
    stack_outputs = models.JSONField(default=dict)
    published_at = models.DateTimeField()
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")
    #: Set when a publish no longer lists it; cleared if a later publish does.
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="capability_unique_per_agent"),
        ]
        ordering = ["agent_id", "name"]

    def __str__(self) -> str:
        return f"capability:{self.agent_id}/{self.name}"


class CapabilityGrant(models.Model):
    """A workspace admin allowing `agent` to lease `capability_name` in `workspace`.

    By NAME, not by FK: an admin may grant before the agent first publishes, and
    a republish (which may retire and re-add) never orphans a grant. The limits
    may only LOWER the manifest's; NULL means "the manifest's own"."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="capability_grants")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="capability_grants")
    capability_name = models.CharField(max_length=40)
    max_concurrent = models.PositiveIntegerField(null=True, blank=True)
    max_lease_s = models.PositiveIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["agent", "capability_name"], name="capability_grant_unique"),
        ]
        ordering = ["agent_id", "capability_name"]


class CapabilityLease(models.Model):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    READY = "ready"
    RELEASED = "released"
    FAILED = "failed"
    EXPIRED = "expired"
    STATUS_CHOICES = [(s, s) for s in (QUEUED, PROVISIONING, READY, RELEASED, FAILED, EXPIRED)]
    #: Holding (or waiting for) a machine.
    LIVE = (QUEUED, PROVISIONING, READY)
    #: Holding a machine — what counts against the cap.
    HOLDING = (PROVISIONING, READY)
    TERMINAL = (RELEASED, FAILED, EXPIRED)

    #: The EC2 ClientToken of this lease's one launch.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="capability_leases")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="capability_leases")
    capability = models.ForeignKey(AgentCapability, on_delete=models.CASCADE, related_name="leases")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=QUEUED)
    params = models.JSONField(default=dict)
    #: The turn this lease is for: when it reaches a terminal status the lease is
    #: released. SET_NULL — a deleted turn leaves the lease to its other guards.
    turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="capability_leases")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="+")
    instance_id = models.CharField(max_length=32, blank=True, default="")
    # The limits in force for THIS lease (manifest lowered by the grant), fixed at acquire.
    max_lease_s = models.PositiveIntegerField()
    idle_release_s = models.PositiveIntegerField()
    ready_timeout_s = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    provisioning_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    #: Set when the machine is launched: provisioning_at + max_lease_s.
    expires_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField()
    #: The machine's ready.json, once read.
    ready_info = models.JSONField(default=dict, blank=True)
    #: Why it ended (or why a launch is being retried).
    reason = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["capability", "status", "created_at"], name="cap_lease_queue_idx"),
            models.Index(fields=["status"], name="cap_lease_status_idx"),
        ]

    def __str__(self) -> str:
        return f"lease:{self.pk}({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in self.LIVE
