"""Pydantic schemas for /api/contacts."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class ContactOut(StrictModel):
    """A person one workspace knows about."""

    id: int
    email: str
    display_name: str = ""
    workspace_id: str
    is_user: bool = Field(
        description="Whether this person has authenticated for real and been "
                    "linked to a canopy account. Still says nothing about "
                    "membership — being known and being let in are different.",
    )
    auth_result: str = Field(
        description='Best email-authentication grade ever seen from this '
                    'address: "dmarc" (the visible From: was not forged), '
                    '"dkim", "spf" (envelope only — weak), or "none". A grade '
                    'rather than a boolean because partner organisations run '
                    'mail of varying quality and "unverified" must stay a '
                    'workable state. Even "dmarc" proves the DOMAIN sent it, '
                    'not which human.',
    )
    last_auth_result: str = Field(
        description="Grade on the most recent message. Differs from "
                    "`auth_result` when a correspondent who used to "
                    "authenticate no longer does, which is worth noticing.",
    )
    notes: str = ""
    attributes: dict = Field(
        default_factory=dict,
        description="Cached facts from other systems. Never authoritative: "
                    "agent execution calls those systems and honours their "
                    "ACLs live, so nothing here may be what grants.",
    )
    message_count: int = 0
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class ContactPatchIn(StrictModel):
    """What a human may correct. Deliberately narrow.

    `email` is absent on purpose: it is the identity the record is keyed on, and
    editing it would silently re-attribute a correspondence history to someone
    else. The auth grades are absent because they are the mail server's verdict,
    not an opinion.
    """

    display_name: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    attributes: dict | None = None
