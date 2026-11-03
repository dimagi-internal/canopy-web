"""Private runner and direct-turn schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from ninja import Schema
from pydantic import Field


class PrivateRunnerIn(Schema):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(default="", max_length=200)
    # The runner app on this same machine, when there is one (a Runner you own): links them.
    runner: uuid.UUID | None = None


class PrivateRunnerOut(Schema):
    id: uuid.UUID
    name: str
    host: str
    # The Runner holding this machine's device keys: its runner app when linked,
    # else its own `computer` runner. None only on a retired registration.
    runner_id: uuid.UUID | None = None
    created_at: dt.datetime
    last_seen_at: dt.datetime | None = None
    retired_at: dt.datetime | None = None


class LocalTurnIn(Schema):
    """Open (or re-open) the direct turn for one Claude Code session."""

    agent: str = Field(min_length=1, max_length=100)
    private_runner: uuid.UUID
    claude_session_id: str = Field(min_length=1, max_length=100)
    cwd: str = Field(default="", max_length=300)
    #: "auto" is honoured only for an agent admin; anything else runs manual.
    turn_mode: Literal["", "manual", "auto"] = ""
    #: Open the session as a LOWER role ("editor" / "viewer"), as `TurnIn.acts_as`.
    acts_as: Literal["", "editor", "viewer"] | None = None


class LocalTurnOut(Schema):
    turn_id: uuid.UUID
    created: bool
    agent: str
    workspace: str
    turn_mode: str
    turn_mode_basis: str
    lease_expires_at: dt.datetime | None = None
    #: Where the person reads this turn on canopy-web.
    url: str
    #: The caller envelope, as a runner would have received it at claim.
    envelope: dict[str, Any]


class LocalTurnRenewOut(Schema):
    turn_id: uuid.UUID
    status: str
    lease_expires_at: dt.datetime | None = None


class PrivateRunnerUseOut(Schema):
    """A private runner as the runners page lists it: whose, which agents, how much."""

    id: uuid.UUID
    name: str
    owner_email: str
    owner_name: str
    agents: list[str]
    local_turns: int
    last_seen_at: dt.datetime | None = None
    retired: bool = False
