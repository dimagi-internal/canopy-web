"""Pydantic schemas for the /api/tokens/ surface."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel

#: Longest expiry a token may be minted with. 0 (never expires) covers longer.
MAX_TTL_DAYS = 36_500


class PersonalTokenOut(StrictModel):
    """A token as listed to its owner. Never contains the raw value."""
    id: int
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    last_used_client: str = ""
    """What last used it: the caller's User-Agent (a runner sends its host)."""
    revoked_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    """When the token stops authenticating; null means it never expires."""


class PersonalTokenCreateIn(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    # The ceiling is a validation bound, not a policy cap: past it the expiry
    # date overflows (`timedelta` / year 9999) and the mint 500s. A caller who
    # asks for more is refused with a 422, never quietly handed less.
    ttl_days: int | None = Field(
        default=None,
        ge=0,
        le=MAX_TTL_DAYS,
        description=(
            "Days until this token expires. Omit for the server default "
            "(PAT_DEFAULT_TTL_DAYS, 180). 0 means it never expires. At most "
            f"{MAX_TTL_DAYS} (100 years); a larger value is refused, not shortened."
        ),
    )


class PersonalTokenCreatedOut(PersonalTokenOut):
    """Returned exactly once at creation — includes the raw token."""
    raw: str


class WsTicketOut(StrictModel):
    #: Put on the socket URL as `?ticket=`; good for one socket, for `expires_in` seconds.
    ticket: str
    expires_in: int
