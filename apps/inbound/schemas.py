"""Pydantic v2 schemas for /api/inbound."""
from __future__ import annotations

from pydantic import BaseModel


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
