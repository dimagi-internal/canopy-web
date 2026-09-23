"""Pydantic schemas for /api/contacts."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class ContactOut(StrictModel):
    """A person one workspace knows about."""

    id: int
    identity: str = Field(
        description="What canopy actually matched on: the address for someone "
                    "who wrote in, or `<site>:<their id>` for someone using an "
                    "embedded agent. Prefer this over `email` when showing who "
                    "a contact is — an embed contact's address is a claim the "
                    "site made, not the key.",
    )
    source: str = Field(
        description='How canopy came to know this person: "email" (wrote to an '
                    'agent\'s inbox) or "embed" (used an agent on a connected '
                    'site). It says which field is the identity.',
    )
    email: str = ""
    external_id: str = Field(
        default="",
        description="The connected site's own id for this person, opaque to "
                    "canopy. Scoped to that site, so it cannot collide with a "
                    "canopy user or with another site's people.",
    )
    app_name: str = Field(
        default="", description="The connected site that vouched for them, if any.",
    )
    display_name: str = ""
    workspace_id: str
    is_user: bool = Field(
        description="Whether this person has authenticated for real and been "
                    "linked to a canopy account. Still says nothing about "
                    "membership — being known and being let in are different.",
    )
    auth_result: str = Field(
        description='Best grade ever seen for this person, on one ladder shared '
                    'by every channel. Email: "dmarc" (the visible From: was '
                    'not forged), "dkim", "spf" (envelope only — weak). '
                    'Embedded sites: "app_signed", "app_secret" (proves the '
                    'site, not the person) — a visitor vouched for by a '
                    'host cannot exceed "app_signed", because canopy '
                    'verifies the host, not the human. "none" '
                    'either way. A grade rather than a boolean because the '
                    'people canopy deals with arrive through systems of wildly '
                    'varying quality and "unverified" must stay a workable '
                    'state. Even the top grade proves the SENDER authorised it, '
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
    is_blocked: bool = Field(
        default=False,
        description="Whether this person has been refused. Blocking stops them "
                    "reaching an agent without disconnecting the site or "
                    "closing the mailbox, and keeps the record.",
    )
    blocked_reason: str = ""
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime


class ContactPatchIn(StrictModel):
    """What a human may correct. Deliberately narrow.

    `email`, `source` and `external_id` are absent on purpose: they are what the
    record is keyed on, and editing one would silently re-attribute a history to
    someone else. The auth grades are absent because they are the sender's
    verdict, not an opinion.

    `blocked` is here because refusing one person is a human decision, and the
    only alternatives were disconnecting a whole site or closing a mailbox.
    """

    display_name: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    attributes: dict | None = None
    blocked: bool | None = Field(
        default=None, description="Set true to refuse this person, false to allow them again.",
    )
    blocked_reason: str | None = Field(default=None, max_length=200)
