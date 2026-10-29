"""Every route's answer to a `services.ConnectionError`, in one place.

The service layer raises one exception family whose `code` is stable; this maps
each code to a status and a problem+json body (`error` = the code), so the
token routes, the turn routes and the sharing routes cannot drift apart.
"""
from __future__ import annotations

from apps.api.errors import ProblemError

from . import services

# code -> (status, title). 409: the caller's state needs changing (connect,
# reconnect, disconnect one); 400: what the caller sent; 502/503: the provider,
# not the caller.
_PROBLEMS: dict[str, tuple[int, str]] = {
    "invalid_app": (400, "Invalid app settings"),
    "client_id_locked": (409, "Client id in use"),
    "wrong_account": (409, "Wrong account"),
    "not_configured": (409, "Not set up"),
    "not_connected": (409, "Not connected"),
    "reconnect_required": (409, "Reconnect required"),
    "ambiguous": (409, "More than one account connected"),
    "insufficient_scope": (409, "Missing permissions"),
    "own_connection": (409, "Cannot share this connection"),
    "not_grantable": (409, "Cannot share this connection"),
    "provider_error": (502, "Provider error"),
    "provider_unavailable": (503, "Provider unavailable"),
}


def problem_for(exc: services.ConnectionError, *, title: str, owner: str, **members) -> ProblemError:
    """The response for `exc`. `title` is the provider's display name; `owner` is
    whose connection it is, as a possessive ("Your", "eva's"). `members` add to
    the body (`connect_url`, `accounts`). An unexpected code is a 502: it is
    still the provider's or canopy's failure, never the caller's."""
    status, heading = _PROBLEMS.get(exc.code, (502, "Provider error"))
    detail = {
        "not_configured": f"{title} is not set up for this workspace.",
        "client_id_locked": f"Accounts are connected with this {title} client id; remove the app (which "
                            "disconnects them) to switch to another.",
        "wrong_account": f"That {title} account is not one {owner.lower() if owner == 'Your' else owner} "
                         "connection may use; sign in with the right account.",
        "not_connected": f"{owner} {title} is not connected.",
        "reconnect_required": f"{owner} {title} connection no longer works; connect it again.",
        "ambiguous": f"{owner} {title} has more than one account connected; disconnect all but one.",
        "insufficient_scope": f"{title} did not grant every permission canopy needs; connect again "
                              "and allow them all.",
        "provider_unavailable": f"{title} could not be reached; try again.",
        "provider_error": f"{title} refused the request.",
    }.get(exc.code, str(exc))
    return ProblemError(status, heading, detail=detail, members={"error": exc.code or "provider_error", **members})
