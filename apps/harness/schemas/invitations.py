"""Runner invitation and pair-by-role schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from ninja import Schema
from pydantic import Field

from .fleets import RunnerFleetOut

#: A runner name usable as a Runner.name AND inside a CloudFormation stack name —
#: the same rule as `runner/ec2/_lib.sh::runner_name_ok`.
RUNNER_NAME_PATTERN = r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"


RunnerInvitationStatus = Literal["pending", "approved", "consumed", "expired"]


class InvitedRunnerIn(Schema):
    """One runner on the invited machine, named `name`, serving `workspace`'s tree
    (`kind` tree) or only agent `slug` (`kind` agent, an agent of `workspace`).
    `ecdh_public_jwk` is that runner's zero-knowledge ECDH
    key (its KMS key in the `<stack>-ecdh` stack) when known."""

    name: str = Field(pattern=RUNNER_NAME_PATTERN, max_length=63)
    workspace: str = Field(min_length=1, max_length=100)
    kind: Literal["tree", "agent"] = "tree"
    slug: str = Field(default="", max_length=100)
    ecdh_public_jwk: dict[str, Any] | None = None


class RunnerInvitationIn(Schema):
    """Invite one new cloud box, by the name it will pair as and its EC2 instance
    role, into a workspace. `ecdh_public_jwk` is the box's zero-knowledge ECDH
    public key (its `<stack>-ecdh` KMS key) when known, so one approval covers it.
    `runners`: a machine that runs several runners lists them, and the one
    approval pairs every one."""

    name: str = Field(pattern=RUNNER_NAME_PATTERN, max_length=63)
    workspace: str = Field(min_length=1, max_length=100)
    role_arn: str = Field(min_length=1, max_length=300)
    ecdh_public_jwk: dict[str, Any] | None = None
    runners: list[InvitedRunnerIn] | None = Field(default=None, max_length=8)
    # Pair its runners directed-only: a test box (`up.sh --test`).
    directed: bool = False


class InvitationRunnerOut(Schema):
    """One runner an invitation pairs, and its id once the machine has paired."""

    name: str
    workspace: str
    kind: Literal["tree", "agent"]
    slug: str = ""
    has_ecdh_key: bool = False
    runner_id: uuid.UUID | None = None


class RunnerInvitationCheckOut(Schema):
    """You may invite a runner into `workspace`."""

    workspace: str


class RunnerInvitationOut(Schema):
    """A pairing invitation and where it stands. `approve_url` is the page where
    its creator approves it with a passkey; `runner_id` is set once the box has
    paired (status `consumed`)."""

    id: uuid.UUID
    name: str
    workspace: str
    role_arn: str
    status: RunnerInvitationStatus
    approve_url: str
    created_at: dt.datetime
    expires_at: dt.datetime
    created_by_email: str = ""
    # Its runners pair directed-only (a test box).
    directed: bool = False
    approved_at: dt.datetime | None = None
    #: Whether the box's ECDH key is part of what approving covers.
    has_ecdh_key: bool = False
    runner_id: uuid.UUID | None = None
    #: The runners the machine pairs as; empty for a machine with one runner.
    runners: list[InvitationRunnerOut] = []
    #: A FLEET's invitation: what approving it lets join (`fleets.py`). Approving
    #: uses the invitation up; the fleet's instances then join on their own.
    fleet: RunnerFleetOut | None = None


class PairingRunnerIn(Schema):
    """One runner of a machine pairing all its runners at once: its `name`, the
    PUBLIC half of the runner's own signing key (`public_jwk`, a software P-256 key
    in the runner's home), its zero-knowledge ECDH key, and what it declares."""

    name: str = Field(pattern=RUNNER_NAME_PATTERN, max_length=63)
    public_jwk: dict[str, Any]
    ecdh_public_jwk: dict[str, Any] | None = None
    ecdh_store: str = Field(default="", max_length=16)
    capabilities: dict = {}
    host: str = Field(default="", max_length=200)


class RunnerPairByRoleIn(Schema):
    """A new cloud box pairs by proving its EC2 instance role: `proof` is an
    `X-Canopy-Aws-Proof` value (a signed, unsent `sts:GetCallerIdentity`) whose
    htu is this URL. It needs no token. `ecdh_public_jwk`/`ecdh_store` are its
    zero-knowledge ECDH key, as `POST /api/runner-device-keys/` takes them."""

    name: str = Field(pattern=RUNNER_NAME_PATTERN, max_length=63)
    kind: Literal["cloud"] = "cloud"
    capabilities: dict = {}
    host: str = Field(default="", max_length=200)
    proof: str = Field(min_length=1, max_length=20000)
    ecdh_public_jwk: dict[str, Any] | None = None
    ecdh_store: str = Field(default="", max_length=16)
    #: A machine running several runners pairs them all in this one call (`name`
    #: is then the MACHINE's name, the one its invitation was made for).
    runners: list[PairingRunnerIn] | None = Field(default=None, max_length=8)


class RunnerPairByRoleOut(Schema):
    """`paired` (a new runner), `reattached` (this role's existing runner — a
    replaced instance), or `waiting` (the invitation is not approved yet; ask
    again on the next poll). A machine of several runners gets `runners`, each
    runner's id by its name (`paired` when any was made)."""

    status: Literal["paired", "reattached", "waiting"]
    runner_id: uuid.UUID | None = None
    runners: dict[str, uuid.UUID] = {}
    #: Each runner's device key id, by runner name — what the box records so its
    #: proofs are sent (`canopy vault device --registered`).
    device_keys: dict[str, uuid.UUID] = {}


# ---- laptop pairing by browser (apps/harness/api_runner_pairings.py) ------------------
