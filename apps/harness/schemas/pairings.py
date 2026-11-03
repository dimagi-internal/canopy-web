"""Laptop pairing schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from ninja import Schema
from pydantic import Field

from .invitations import RUNNER_NAME_PATTERN

RunnerPairingStatus = Literal["pending", "approved", "consumed", "declined", "expired"]


class RunnerPairingIn(Schema):
    """A box asks to be paired. `public_jwk` is the PUBLIC half of the device key
    it holds (and whose DPoP proof is on this request); `redirect_port` is the
    loopback port the approval's secret is handed back through."""

    public_jwk: dict[str, Any]
    kind: Literal["secure-enclave", "tpm", "software"]
    ecdh_public_jwk: dict[str, Any] | None = None
    ecdh_store: str = Field(default="", max_length=16)
    hostname: str = Field(default="", max_length=100)
    account: str = Field(default="", max_length=100)
    platform: Literal["macos", "windows", "linux"]
    has_emdash: bool = False
    # Its emdash is serving DevTools right now. On Windows only that makes the new
    # runner drive emdash (`api_runner_pairings.drives_emdash`).
    emdash_cdp_live: bool = False
    redirect_port: int = Field(ge=1024, le=65535)
    # The runner this box already is (its runner.json) when its token stopped working.
    reconnect_runner: uuid.UUID | None = None


class RunnerPairingCreatedOut(Schema):
    """A new pairing: show `user_code` and open `verify_url`; poll the claim
    every `interval` seconds until `expires_at`."""

    id: uuid.UUID
    user_code: str
    verify_url: str
    expires_at: dt.datetime
    interval: int


class RunnerPairedOut(Schema):
    """`reattached` (this key is already an active runner's) or `paired` (a new
    runner was made); `waiting` while the pairing is not yet approved."""

    status: Literal["paired", "reattached", "waiting"]
    runner_id: uuid.UUID | None = None
    name: str = ""
    workspace: str = ""
    device_key_id: uuid.UUID | None = None


class RunnerPairingWorkspaceOut(Schema):
    slug: str
    name: str


class RunnerPairingOut(Schema):
    """A pairing as the person approving it sees it. Everything about the box is
    what the box says about itself."""

    user_code: str
    hostname: str
    account: str
    platform: str
    kind: str
    has_emdash: bool
    status: RunnerPairingStatus
    expires_at: dt.datetime
    suggested_name: str
    #: Where the caller may put it: their workspaces where they may run work.
    workspaces: list[RunnerPairingWorkspaceOut]
    runner_id: uuid.UUID | None = None
    # Set when this box asks to reconnect to a runner of YOURS: approving gives that
    # runner its key back, with its name and workspace, instead of making a new one.
    reconnect_name: str = ""


class RunnerPairingApproveIn(Schema):
    workspace: str = Field(min_length=1, max_length=100)
    name: str = Field(pattern=RUNNER_NAME_PATTERN, max_length=63)


class RunnerPairingApprovedOut(Schema):
    """Send the browser to `redirect_url`: the box's own loopback, which takes
    the one-time secret and claims the pairing."""

    status: Literal["approved"]
    redirect_url: str


class RunnerPairingDeclinedOut(Schema):
    status: Literal["declined"]


class RunnerPairingClaimIn(Schema):
    secret: str = Field(default="", max_length=200)


# ---- runner releases (apps/harness/runner_releases.py) --------------------------------
