from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from ninja import Schema


class ListenRunnerOut(Schema):
    id: uuid.UUID
    name: str
    #: Its daemon beat in the last 90 seconds.
    online: bool


class ListenRunnersOut(Schema):
    items: list[ListenRunnerOut]


class ListenHello(Schema):
    #: This session's ephemeral public key (base64url, uncompressed P-256).
    phone_eph: str
    #: The phone's paired public key; null when pairing.
    phone_key: str | None = None
    #: What the laptop shows for this phone.
    label: str = ""


class ListenSessionIn(Schema):
    runner_id: uuid.UUID
    kind: Literal["pair", "meeting"]
    hello: ListenHello


class ListenSessionCreated(Schema):
    id: uuid.UUID


class ListenSessionOut(Schema):
    id: uuid.UUID
    kind: Literal["pair", "meeting"]
    state: Literal["waiting", "live", "ended"]
    end_reason: str
    created_at: dt.datetime


class PendingListenSession(Schema):
    session_id: uuid.UUID
    runner_id: uuid.UUID
    kind: Literal["pair", "meeting"]
    hello: dict


class PendingListenSessionsOut(Schema):
    items: list[PendingListenSession]
