"""Runner device key and runner session schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from ninja import Schema
from pydantic import Field

DeviceKeyKind = Literal["secure-enclave", "tpm", "software", "aws-iam"]


DeviceKeyStatus = Literal["pending", "active", "revoked"]


class RunnerSessionIn(Schema):
    """Exchange a device-key proof for a runner token. The proof rides a header:
    `X-Canopy-Aws-Proof` (an EC2 box's instance role) or `DPoP` (a key the box
    holds), bound to this URL."""
    runner: uuid.UUID


class RunnerSessionOut(Schema):
    """A runner's own API credential: send it as `Authorization: Bearer`. It opens
    the runner protocol only and runs out at `expires_at`; prove the device again
    before then for the next one."""
    access_token: str
    expires_at: dt.datetime
    runner: uuid.UUID


class RunnerDeviceKeyIn(Schema):
    """Register the PUBLIC half of a key the runner holds. Re-posting the same
    key (same RFC 7638 thumbprint) is idempotent.

    `kind: "aws-iam"` (an EC2 box's instance role, canopy-web#1549) sends
    `role_arn` and `proof` (an `X-Canopy-Aws-Proof` value whose htu is this
    URL) instead of `public_jwk`; the proof's signer must BE `role_arn`.

    `ecdh_public_jwk` (zero-knowledge vault, canopy-web#1550) is a SEPARATE
    P-256 public key the box does ECDH with, for every kind including
    `aws-iam`. Sending a new or different one for an `active` key sends it
    back to `pending` — a person approves again."""

    runner: uuid.UUID
    public_jwk: dict[str, Any] | None = None
    ecdh_public_jwk: dict[str, Any] | None = None
    kind: DeviceKeyKind
    label: str = Field(default="", max_length=120)
    role_arn: str = Field(default="", max_length=300)
    proof: str = Field(default="", max_length=20000)
    #: Where the ECDH private key lives ("secure-enclave", "tpm", "aws-kms",
    #: "software"), for people to read on Settings. Self-reported; display only.
    ecdh_store: str = Field(default="", max_length=16)


class DeviceKeyAgentOut(Schema):
    """An agent a device key can open right now, for people to read."""

    slug: str
    name: str


class DeviceKeyVouchOut(Schema):
    """An agent whose vault the caller is a member of but does not manage, that
    their own machine waits on: they sign for the machine (an endorsement) and a
    keeper hands it the vault key (spec 2026-10-22 vault-access-follows-canopy-access §1)."""

    slug: str
    agent_id: str


class RunnerDeviceKeyCreatedOut(Schema):
    id: uuid.UUID
    jkt: str
    status: DeviceKeyStatus
    #: Where a person who owns or administers the runner approves it.
    approve_url: str
    #: RFC 7638 thumbprint of the ECDH key ("" when none was sent).
    ecdh_jkt: str = ""


class RunnerDeviceKeyOut(Schema):
    """A device key and its state. Public material only — there is no secret."""

    id: uuid.UUID
    runner: uuid.UUID
    runner_name: str
    #: The runner's host (`user@machine`) and kind ("emdash", "cloud") — so a
    #: person sees WHICH machine, not just the runner's name.
    runner_host: str = ""
    runner_kind: str = ""
    jkt: str
    kind: DeviceKeyKind
    label: str = ""
    #: `aws-iam` only: the instance role the box proves itself as.
    role_arn: str = ""
    #: The ECDH key vault keys are wrapped to (canopy-web#1550); "" / {} if none.
    ecdh_jkt: str = ""
    ecdh_public_jwk: dict[str, Any] = {}
    #: Pending AGAIN after an approval — its ECDH (encryption) key was added
    #: or changed (canopy-web#1550), not a new box.
    reapproval: bool = False
    #: Active and signing in, but its new ECDH key waits for approval: it
    #: cannot read zero-knowledge agents until a person approves it.
    ecdh_waiting: bool = False
    status: DeviceKeyStatus
    created_at: dt.datetime
    approved_at: dt.datetime | None = None
    approved_by_email: str = ""
    revoked_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    #: The caller owns or administers this key's runner (may approve / revoke).
    can_manage: bool = False
    #: On approve only: zero-knowledge agents the caller administers that this
    #: key now waits on a vault-key grant for (offer "Grant" right away).
    zk_pending_agents: list[str] = []
    #: Zero-knowledge agents whose vault the caller is a MEMBER of (not a manager)
    #: that this key, the caller's own machine, waits on: approving it signs for it,
    #: and a keeper passes the vault key on.
    zk_vouch_agents: list[DeviceKeyVouchOut] = []
    #: Where the box says its ECDH private key lives (see RunnerDeviceKeyIn).
    ecdh_store: str = ""
    #: On the list: agents the caller administers whose secrets this key can
    #: open right now ("can open Hal, Eva").
    can_open: list[DeviceKeyAgentOut] = []


# ---- the box's usage report (apps/harness/usage.py) ---------------------------
