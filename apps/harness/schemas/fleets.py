"""Runner fleet schemas."""
from __future__ import annotations

import datetime as dt
import uuid

from ninja import Schema
from pydantic import Field


class RunnerFleetMemberOut(Schema):
    id: uuid.UUID
    name: str
    instance_id: str
    #: Where the scaler has it: on / draining / stopped (warm pool) / waking.
    power: str
    live_status: str


class RunnerFleetOut(Schema):
    """A runner fleet: one EC2 Auto Scaling group whose instances join as its
    members once its owner approved it, and whose size canopy-web sets from the
    work waiting for it."""

    id: uuid.UUID
    name: str
    workspace: str
    #: pending (waiting for its owner's approval) / active / retired.
    status: str
    role_arn: str
    region: str
    group_name: str
    min_members: int
    max_members: int
    #: Turns one member runs at once.
    member_turns: int
    #: The credential set every member reads, and the vault its values are in
    #: (made with the first member).
    credential_set_id: uuid.UUID
    credential_vault_id: uuid.UUID | None = None
    has_ecdh_key: bool = False
    approved_at: dt.datetime | None = None
    owner_email: str = ""
    members: list[RunnerFleetMemberOut] = []


class RunnerFleetIn(Schema):
    """A new fleet (or the same pending one, asked again): `fleet.sh up` sends it
    once its stack exists. Nothing joins until its creator approves it."""

    name: str = Field(..., pattern=r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$")
    workspace: str
    role_arn: str
    ecdh_public_jwk: dict | None = None
    region: str = Field("us-east-1", pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    group_name: str = Field(..., pattern=r"^[A-Za-z0-9_+=,.@/-]+$", max_length=255)
    min_members: int = Field(1, ge=1, le=50)
    max_members: int = Field(4, ge=1, le=50)
    member_turns: int = Field(2, ge=1, le=32)
    #: One of your credential sets for the members to read (other runners may read it
    #: too, in this tree or another); omitted, the fleet gets a new one.
    credential_set: uuid.UUID | None = None


class RunnerFleetSizeIn(Schema):
    min_members: int = Field(..., ge=1, le=50)
    max_members: int = Field(..., ge=1, le=50)
