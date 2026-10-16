"""Pydantic v2 schemas for the capability routes.

The manifest a publish sends is `canopy_runtime.schema.CapabilityManifest` itself
— the model the agent's `canopy-runtime publish-capabilities` validated it with —
so the two sides share one definition of a manifest.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from canopy_runtime.schema import CapabilityManifest
from pydantic import Field

from apps.common.schemas import StrictModel

LeaseStatus = Literal["queued", "provisioning", "ready", "released", "failed", "expired"]


class AgentCapabilitiesIn(StrictModel):
    """The agent's WHOLE declared list: a capability left out is retired."""

    capabilities: list[CapabilityManifest]


class CapabilityGrantOut(StrictModel):
    id: int
    workspace: str
    agent: str
    capability: str
    max_concurrent: int | None
    max_lease_s: int | None
    created_by: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class CapabilityLeaseOut(StrictModel):
    lease_id: str
    agent: str
    capability: str
    status: LeaseStatus
    #: status == "ready": the machine is up and exec will be accepted.
    ready: bool
    #: 1-based place in the FIFO queue while status == "queued", else null.
    position: int | None
    instance_id: str | None
    params: dict[str, Any]
    turn_id: str | None
    created_at: dt.datetime
    provisioning_at: dt.datetime | None
    ready_at: dt.datetime | None
    ended_at: dt.datetime | None
    expires_at: dt.datetime | None
    last_heartbeat_at: dt.datetime
    #: Seconds without a heartbeat (any lease call) before the lease is released.
    idle_release_s: int
    #: Why it ended, or why its launch is being retried.
    reason: str
    #: The machine's ready.json once read: {"ok", "reason"?, "timings"?, "cli_version"?};
    #: for a pool lease, {"slot": "<name>"} once it holds one.
    ready_info: dict[str, Any]
    #: A `pool` lease's slot once assigned (status ready; also `params.slot` and
    #: `ready_info.slot`), else null — always null for an ec2-ephemeral lease.
    slot: str | None
    #: Where the machine writes artifacts (the same value as CANOPY_LEASE_OUT in an
    #: exec), for an argv that needs it literally: s3://<bucket>/vm/<role-id>:<instance-id>/lease/<id>/out/
    #: on a current stack — under the machine's own prefix, so "" until the machine
    #: is launched — or s3://<bucket>/leases/<id>/out/ on a stack from before 0.9.0.
    #: Always "" for a pool lease.
    out_prefix: str
    #: A `pool` lease's slot data, decrypted — `{"public": {...}, "secret": {...}}` —
    #: ONLY on the acquire response and GET /leases/{id}, ONLY to the user who
    #: acquired the lease, ONLY while it is `ready`, and only when the slot has
    #: data; null everywhere else (lists, releases, other callers, admins).
    slot_data: dict[str, dict[str, str]] | None = None


class AgentCapabilityOut(StrictModel):
    name: str
    kind: str
    description: str
    region: str
    stack: str
    manifest: dict[str, Any]
    stack_outputs: dict[str, str]
    published_at: dt.datetime
    #: The grant that allows it in the agent's workspace, or null (acquire → 403).
    grant: CapabilityGrantOut | None
    #: The manifest's limits lowered by the grant: what an acquire gets now.
    limits: dict[str, int]
    live_leases: list[CapabilityLeaseOut]


class AgentCapabilitiesOut(StrictModel):
    agent: str
    capabilities: list[AgentCapabilityOut]
    #: PUT: the names this publish retired. GET: every retired name.
    retired: list[str]


class CapabilityGrantIn(StrictModel):
    agent: str
    capability: str = Field(pattern=r"^[a-z0-9-]{3,40}$")
    #: Lower the manifest's ceilings (never raise them); null = the manifest's.
    max_concurrent: int | None = Field(default=None, ge=1)
    max_lease_s: int | None = Field(default=None, ge=60)


class CapabilityLeaseIn(StrictModel):
    agent: str
    capability: str
    #: The manifest's params. A pool takes only an optional `slot` (one of its
    #: declared slots): wait for exactly that slot instead of the first free one.
    params: dict[str, Any] = Field(default_factory=dict)
    #: Bind the lease to a turn (CANOPY_TURN_ID): it is released when the turn ends.
    turn_id: str | None = None


class CapabilityExecIn(StrictModel):
    #: argv[0] must be on the manifest's exec.allow; every element is shell-quoted.
    argv: list[str] = Field(min_length=1)
    timeout_s: int = 600


class CapabilityExecOut(StrictModel):
    lease_id: str
    #: The SSM CommandId: read the result with GET …/exec/{exec_id}.
    exec_id: str
    #: The timeout applied (capped at the time the lease has left).
    timeout_s: int


class CapabilityLeaseListOut(StrictModel):
    leases: list[CapabilityLeaseOut]
    #: True when more leases matched than `limit` returned.
    truncated: bool


class CapabilitySpendRow(StrictModel):
    agent: str
    capability: str
    #: Leases that held a machine at some point in the window.
    leases: int
    #: Seconds of machine time in the window: each lease from launch
    #: (`provisioning_at`) to `ended_at`, or now while it still holds one.
    machine_seconds: int
    #: Of those leases, how many hold a machine now.
    live: int


class CapabilitySpendOut(StrictModel):
    since: dt.datetime
    until: dt.datetime
    rows: list[CapabilitySpendRow]
    total_machine_seconds: int


class CapabilityForceReleaseIn(StrictModel):
    #: Why, for the lease's `reason` and the audit trail.
    reason: str = Field(default="", max_length=300)


class CapabilityOrphansOut(StrictModel):
    agent: str
    capability: str
    #: Tagged instances with no live lease, terminated now.
    orphans: int
    #: Ready leases whose machine was found gone, ended now.
    gone: int


class CapabilityExecStatusOut(StrictModel):
    exec_id: str
    #: SSM's own status: Pending | InProgress | Delayed | Success | Cancelled |
    #: TimedOut | Failed | Cancelling.
    status: str
    done: bool
    #: The script's exit code once done, else null.
    exit_code: int | None
    stdout_tail: str
    stderr_tail: str
    #: Presigned GETs of the full streams (up to 1 h: never longer than canopy-web's
    #: signing credentials last), once done; else null.
    stdout_url: str | None
    stderr_url: str | None


class CapabilityUploadsIn(StrictModel):
    names: list[str] = Field(min_length=1)


class CapabilityUploadOut(StrictModel):
    name: str
    #: Presigned PUT (valid `expires_in_s`). Upload the file's bytes to it.
    put_url: str
    #: Where it lands, to pass in an exec's argv.
    s3_uri: str


class CapabilityUploadsOut(StrictModel):
    lease_id: str
    uploads: list[CapabilityUploadOut]
    #: How long the URLs work: up to 3600, less when canopy-web's credentials expire sooner.
    expires_in_s: int


class CapabilityArtifactOut(StrictModel):
    #: Relative to the lease's out/ prefix (`out_prefix`).
    path: str
    size: int
    #: Presigned GET (valid `expires_in_s`).
    url: str


class CapabilityArtifactsOut(StrictModel):
    lease_id: str
    prefix: str
    artifacts: list[CapabilityArtifactOut]
    #: How long the URLs work: up to 3600, less when canopy-web's credentials expire sooner.
    expires_in_s: int


class CapabilitySlotDataIn(StrictModel):
    """Per-key upsert of one pool slot's data. In each section a key set to a
    string is written, a key set to null is deleted, and a key left out is kept;
    a section left out is untouched. Values are non-blank strings (≤ 4096); keys
    match [A-Za-z0-9_.-]{1,64}; a key is public or secret, never both."""

    public: dict[str, str | None] | None = None
    #: Encrypted at rest; never returned by any read except to a ready lease's holder.
    secret: dict[str, str | None] | None = None


class CapabilitySlotDataOut(StrictModel):
    """A slot's data, MASKED: public values, secret key NAMES — never a secret value."""

    slot: str
    #: Whether the manifest declares this slot now (data for an undeclared slot
    #: is kept, never delivered).
    declared: bool
    public: dict[str, str]
    secret_keys: list[str]
    updated_at: dt.datetime | None
    updated_by: str | None
    #: A live lease holds this slot now.
    held: bool
    lease_id: str | None


class CapabilitySlotsOut(StrictModel):
    agent: str
    capability: str
    #: One per declared slot, in declared order.
    slots: list[CapabilitySlotDataOut]
