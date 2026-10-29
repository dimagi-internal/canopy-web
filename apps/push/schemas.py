"""Pydantic schemas for /api/push."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class VapidKeyOut(StrictModel):
    public_key: str


class PushSubscribeIn(StrictModel):
    endpoint: str
    # p256dh/auth map to fixed-width columns (200/100) and are exact crypto values —
    # bound them at the boundary so an over-length value is a 422, not a Postgres-only
    # 500. (user_agent is informational and stays leniently truncated in the view.)
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)
    user_agent: str = ""


class PushUnsubscribeIn(StrictModel):
    endpoint: str


AlertMail = Literal["all", "assigned"]


class NotificationPreferenceOut(StrictModel):
    session_idle_minutes: int
    alert_mail: AlertMail = "all"


class NotificationPreferenceIn(StrictModel):
    # Each field is optional: send only what changes.
    # 0 = off. Capped at a day: past that "it went quiet" is no longer news.
    session_idle_minutes: int | None = Field(default=None, ge=0, le=1440)
    alert_mail: AlertMail | None = None
