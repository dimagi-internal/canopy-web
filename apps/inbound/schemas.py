"""Pydantic v2 schemas for /api/inbound."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel

from apps.common.schemas import StrictModel


class PushEnvelopeIn(BaseModel):
    """The Pub/Sub push envelope: ``{message: {data, messageId, ...}, subscription}``.

    Deliberately NOT a ``StrictModel``. Every other schema here forbids unknown
    fields so a typo 422s instead of being silently ignored — but this body is
    authored by Google, not by us, and Pub/Sub is free to add envelope fields
    whenever it likes. A strict model would turn that into a 422, which Pub/Sub
    reads as "redeliver", turning a cosmetic change into a retry storm.
    """

    message: dict = {}
    subscription: str = ""


class PushResultOut(BaseModel):
    ok: bool
    reason: str = ""
    rang: list[str] = []


class WatchReportIn(StrictModel):
    """What something that armed a Gmail watch reports back.

    ``expires_at`` may be null — that is how you say "this mailbox has no watch",
    which is different from never having reported. Both are honest states and the
    log distinguishes them.

    ``error`` is the third state: the runner is supposed to be watching this
    mailbox and cannot (a revoked grant, a dead OAuth client). Null expiry plus a
    blank error is the retraction a paused runner sends.
    """

    address: str
    expires_at: dt.datetime | None = None
    error: str = ""


class WatchReportOut(StrictModel):
    ok: bool
    reason: str = ""
    expires_at: str = ""


# ── configuration (the UI's surface) ─────────────────────────────────────────


class PushConfigIn(StrictModel):
    audience: str = ""
    service_account: str = ""
    watch_topic: str = ""


class PushConfigOut(StrictModel):
    workspace: str
    audience: str
    service_account: str
    watch_topic: str
    push_url: str
    """The endpoint to paste into the Pub/Sub subscription. Server-computed so
    the UI never has to guess the deployment's own address — getting it wrong is
    silent (pushes go nowhere) and was previously a hand-copied value."""
    verifies: bool
    """False when no audience is set — this workspace refuses every push."""
    updated_at: str = ""


class MailboxIn(StrictModel):
    address: str
    agent_slug: str
    enabled: bool = True


class MailboxPatchIn(StrictModel):
    enabled: bool | None = None
    agent_slug: str | None = None


class MailboxReaderOut(StrictModel):
    """One runner that could pick up a mailbox's mail, and whether it can read it."""

    runner: str
    status: str
    """The runner's ``live_status`` — ``online`` | ``paused`` | ``stale`` | …."""
    can_read: bool | None
    """What the runner's own probe REPORTS. None = it has never reported (an
    older runner) — unknown, and still rung by the doorbell."""
    checked_at: str = ""
    """When that report last arrived; blank when never."""


class MailboxOut(StrictModel):
    id: int
    address: str
    agent_slug: str
    workspace: str
    enabled: bool
    last_push_at: str = ""
    watch_expires_at: str = ""
    watch_error: str = ""
    """Why the runner cannot arm this mailbox's watch; blank when it can."""

    watch_state: str
    """``failed`` | ``armed`` | ``expiring`` | ``expired`` | ``none`` — computed
    server-side so the UI and the event log cannot disagree about what counts as
    healthy."""

    readers: list[MailboxReaderOut] = []
    """The runners that route this agent's email, plus any other box in the
    workspace that reports it can read this address."""


class MailboxListOut(StrictModel):
    items: list[MailboxOut]


class RunnerMailboxOut(StrictModel):
    """A mailbox a runner may read: which address, for which agent, and the
    topic to arm its watch on (blank = this workspace arms no watches)."""

    address: str
    agent_slug: str = ""
    watch_topic: str


class RunnerMailboxListOut(StrictModel):
    items: list[RunnerMailboxOut]
