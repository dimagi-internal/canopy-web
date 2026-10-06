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
