"""Cloud capabilities: machines an agent leases for as long as it needs them.

FRAMEWORK tier. canopy-web owns the generic half — the manifest an agent
publishes, the workspace admin's grant, and the lease lifecycle over AWS — and
nothing domain-specific: the stack, the image and the CLI on it are the agent's
(docs/architecture/capabilities.md).

Two kinds (`AgentCapability.kind`): `ec2-ephemeral`, one fresh EC2 instance per
lease; and `pool`, one NAMED SLOT of a small fixed set per lease — an exclusive
checkout with no machine and no AWS call (`CapabilityLease.slot`).

Three rows:

- ``AgentCapability``: what the agent DECLARED (its ``capability.yaml``, published
  by ``canopy-runtime publish-capabilities``), plus the stack Outputs canopy-web
  resolved when it was published. Never deleted: a capability left out of a
  publish is RETIRED, so its leases keep their history.
- ``CapabilityGrant``: a workspace admin ALLOWING the agent to use it, optionally
  lowering its limits. No grant, no lease.
- ``CapabilityLease``: one machine for one use. Its id is the EC2 ClientToken,
  so a retried launch can never start a second instance.
- ``CapabilitySlotData``: what a `pool` slot IS — e.g. a test worker's phone,
  name, PIN and backup code — stored beside the lease that checks it out, its
  secret half encrypted at rest and handed only to the holder of a ready lease.

Deleting a lease row while it holds a machine (an agent, workspace or
capability deleted under it, which CASCADE reaches) terminates the machine
first: ``signals.py``'s ``pre_delete`` receiver. The stack's own backstop
Lambda covers a terminate that fails.
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
    #: The stack's Outputs (DescribeStacks): LaunchTemplateId, ArtifactsBucketName,
    #: InstanceRoleArn, InstanceRoleId, Capability, Owner, and on a 0.9.0+ stack
    #: LeaseObjectScope, ExecDocumentName, ExecAllow, MaxInstanceLifetimeSeconds,
    #: BackstopFunctionName. Read at publish and re-read by the sweep, after a
    #: ready-timeout, and on POST …/capabilities/{name}/refresh.
    stack_outputs = models.JSONField(default=dict)
    #: When `stack_outputs` was last read from the stack.
    outputs_refreshed_at = models.DateTimeField(null=True, blank=True)
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
    #: Every exec this lease sent: {SSM CommandId: {"sent_at": ISO, "timeout_s":
    #: int, "done": bool}} (rows from before 0.9.0 hold a bare ISO sent-at). How
    #: exec status tells "SSM has not materialised it yet" (Pending) from "not
    #: this lease's", and how a running exec keeps its lease from going idle.
    execs = models.JSONField(default=dict, blank=True)
    #: This lease's object prefix in the stack's bucket, no trailing slash:
    #: vm/<InstanceRoleId>:<instance-id>/lease/<id> on a 0.9.0+ stack (set when
    #: the instance is recorded), leases/<id> on an older one (set at acquire).
    #: in/, out/ and exec/ live under it. Fixed for the lease's life.
    s3_base = models.CharField(max_length=300, blank=True, default="")
    #: Why it ended (or why a launch is being retried).
    reason = models.CharField(max_length=500, blank=True, default="")
    #: A `pool` lease's slot, set when one is assigned (the lease goes straight
    #: to ready); "" while queued and for an ec2-ephemeral lease. A queued pool
    #: lease that asked for a particular slot carries it in `params.slot`.
    slot = models.CharField(max_length=40, blank=True, default="")

    class Meta:
        ordering = ["created_at"]
        constraints = [
            # A slot is held by at most one live lease: the pool's exclusivity,
            # enforced by the database and not only by `promote`'s row lock.
            models.UniqueConstraint(
                fields=["capability", "slot"], name="cap_lease_one_live_per_slot",
                condition=models.Q(status__in=["queued", "provisioning", "ready"]) & ~models.Q(slot=""),
            ),
        ]
        indexes = [
            models.Index(fields=["capability", "status", "created_at"], name="cap_lease_queue_idx"),
            models.Index(fields=["status"], name="cap_lease_status_idx"),
            models.Index(fields=["workspace", "-created_at"], name="cap_lease_ws_created_idx"),
        ]

    def __str__(self) -> str:
        return f"lease:{self.pk}({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status in self.LIVE


class CapabilitySlotData(models.Model):
    """The details of one `pool` slot: a `public` map anyone who may see the
    agent may read, and a `secret` map that is ENCRYPTED AT REST and delivered
    only to the user who acquired a ready lease on that slot (docs/architecture/
    capabilities.md § 6, "Slot data").

    Keyed by the capability ROW and the slot NAME, not by the manifest, so a
    republish (which rewrites `manifest`) never touches it; data for a slot the
    manifest no longer declares is kept but never delivered.

    Write-only for the secret half, like `agents.AgentCredential`: no route
    returns a secret value except the lease read of its holder, and nothing —
    `__str__`, events, errors, the settings audit — ever carries one.
    """

    capability = models.ForeignKey(AgentCapability, on_delete=models.CASCADE, related_name="slot_data")
    slot = models.CharField(max_length=40)
    #: {key: value} (str → str), readable by the agent's workspace members.
    public = models.JSONField(default=dict, blank=True)
    #: Fernet ciphertext (apps/common/encryption.py) of a JSON object {key: value}
    #: (str → str). Never plaintext; "" when there is no secret half.
    secret_enc = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["capability", "slot"], name="cap_slot_data_unique"),
        ]
        ordering = ["capability_id", "slot"]

    def __str__(self) -> str:
        # Never a value: names only.
        return f"slot-data:{self.capability_id}/{self.slot}"

    def __repr__(self) -> str:
        return f"<CapabilitySlotData {self.capability_id}/{self.slot}>"
