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
    #: The machine's ready.json once read: {"ok", "reason"?, "timings"?, "cli_version"?}.
    ready_info: dict[str, Any]
    #: s3://<bucket>/leases/<id>/out/ — where the machine writes artifacts (the same
    #: value as CANOPY_LEASE_OUT in an exec), for an argv that needs it literally.
    out_prefix: str


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
    #: Presigned GETs of the full streams (1 h), once done; else null.
    stdout_url: str | None
    stderr_url: str | None


class CapabilityUploadsIn(StrictModel):
    names: list[str] = Field(min_length=1)


class CapabilityUploadOut(StrictModel):
    name: str
    #: Presigned PUT (1 h). Upload the file's bytes to it.
    put_url: str
    #: Where it lands, to pass in an exec's argv.
    s3_uri: str


class CapabilityUploadsOut(StrictModel):
    lease_id: str
    uploads: list[CapabilityUploadOut]
    expires_in_s: int


class CapabilityArtifactOut(StrictModel):
    #: Relative to leases/<id>/out/.
    path: str
    size: int
    #: Presigned GET (1 h).
    url: str


class CapabilityArtifactsOut(StrictModel):
    lease_id: str
    prefix: str
    artifacts: list[CapabilityArtifactOut]
    expires_in_s: int
