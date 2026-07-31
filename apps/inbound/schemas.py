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
