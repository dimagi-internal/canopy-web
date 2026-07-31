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
    """

    address: str
    expires_at: dt.datetime | None = None


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


class MailboxOut(StrictModel):
    id: int
    address: str
    agent_slug: str
    workspace: str
    enabled: bool
    last_push_at: str = ""
    watch_expires_at: str = ""
    watch_state: str
    """``armed`` | ``expiring`` | ``expired`` | ``none`` — computed server-side so
    the UI and the event log cannot disagree about what counts as healthy."""


class MailboxListOut(StrictModel):
    items: list[MailboxOut]


class RunnerMailboxOut(StrictModel):
    """What a runner needs to arm a watch: which address, on which topic."""

    address: str
    watch_topic: str


class RunnerMailboxListOut(StrictModel):
    items: list[RunnerMailboxOut]
