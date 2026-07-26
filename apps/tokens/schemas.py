"""Pydantic schemas for the /api/tokens/ surface."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class PersonalTokenOut(StrictModel):
    """A token as listed to its owner. Never contains the raw value."""
    id: int
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    """When the token stops authenticating; null means it never expires."""


class PersonalTokenCreateIn(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    ttl_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Days until this token expires. Omit for the server default "
            "(PAT_DEFAULT_TTL_DAYS, 180). 0 means it never expires. There is no "
            "upper bound — with 0 available, a cap would only hand a caller a "
            "shorter token than they asked for without telling them."
        ),
    )


class PersonalTokenCreatedOut(PersonalTokenOut):
    """Returned exactly once at creation — includes the raw token."""
    raw: str
