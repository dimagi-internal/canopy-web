from datetime import datetime
from typing import Literal

from ninja import Schema
from pydantic import EmailStr, Field


class BetaRequestIn(Schema):
    email: EmailStr
    reason: str = Field(min_length=1, max_length=2000)
    # A field no person fills in: the form hides it, and a bot that fills every
    # input gets an ordinary success response and nothing is recorded.
    website: str = ""


class BetaRequestOut(Schema):
    ok: bool


class BetaRequestDetailOut(Schema):
    id: int
    email: str
    reason: str
    created_at: datetime
    status: str  # pending | invited | declined
    workspace: str | None = None  # slug the invite went to
    workspace_name: str | None = None
    role: str = ""
    decided_by: str | None = None  # email
    decided_at: datetime | None = None
    # Only on the invite response: whether the invite email went out
    # (sent | throttled | not_configured | failed). The invite exists either way.
    email_status: str | None = None


class BetaRequestInviteIn(Schema):
    workspace: str
    # As for a workspace access request: never owner — ownership is handed over
    # on the Members list, not as the answer to a request.
    role: Literal["viewer", "editor", "admin"]
