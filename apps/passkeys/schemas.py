from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import Field

from apps.common.schemas import StrictModel


class PasskeyOut(StrictModel):
    id: int
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    #: Synced through a password manager (iCloud Keychain…) rather than device-bound.
    backed_up: bool


class PasskeyOptionsOut(StrictModel):
    #: PublicKeyCredentialCreationOptions / RequestOptions in their JSON form
    #: (binary fields base64url) — what navigator.credentials.create/get take
    #: once those fields are decoded.
    options: dict[str, Any]


class PasskeyRegisterIn(StrictModel):
    #: The browser's PublicKeyCredential, serialized to JSON (base64url fields).
    credential: dict[str, Any]
    label: str = Field(default="", max_length=120)


class PasskeyAssertIn(StrictModel):
    credential: dict[str, Any]


class StepUpStatusOut(StrictModel):
    #: A passkey was confirmed on this browser session within the window.
    active: bool
    until: dt.datetime | None = None
    has_passkey: bool
    #: Only a person at a browser can step up; a token never can.
    browser: bool
    window_seconds: int
