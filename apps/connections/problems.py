"""Every route's answer to a `services.ConnectionError`, in one place.

The service layer raises one exception family whose `code` is stable; this maps
each code to a status and a problem+json body (`error` = the code), so the
token routes, the turn routes and the sharing routes cannot drift apart.
"""
from __future__ import annotations

from apps.api.errors import ProblemError

from . import services, wire

# code -> (status, title). 409: the caller's state needs changing (connect,
# reconnect, disconnect one); 400: what the caller sent; 502/503: the provider,
# not the caller.
_PROBLEMS: dict[str, tuple[int, str]] = {
    "invalid_app": (400, "Invalid app settings"),
    "app_locked": (409, "App in use"),
    "wrong_account": (409, "Wrong account"),
    "not_configured": (409, "Not set up"),
    "not_connected": (409, "Not connected"),
    "ambiguous": (409, "More than one account connected"),
    "insufficient_scope": (409, "Missing permissions"),
    "no_vault_key": (409, "No vault key"),
    "owner_changed": (409, "Connected with another vault key"),
    "revoking": (409, "Being disconnected"),
    "server_use_off": (409, "canopy-web may not use this"),
    "waiting": (503, "Another refresh is under way"),
    "server_refused": (409, "canopy-web could not trust this connection"),
    "needs_share": (409, "Share it again to change this"),
    "bad_canopy_web": (422, "canopy-web cannot be given this"),
    "try_again": (503, "Try again in a moment"),
    "provider_error": (502, "Provider error"),
    "provider_unavailable": (503, "Provider unavailable"),
}


def problem_for(exc: services.ConnectionError, *, title: str, owner: str, detail: str | None = None,
                **members) -> ProblemError:
    """The response for `exc`. `title` is the provider's display name; `owner` is
    whose connection it is, as a possessive ("Your", "eva's"). `members` add to
    the body (`connect_url`, `accounts`); `detail` replaces the standard
    sentence when the caller knows more of the case. A code with no standard sentence
    (`app_locked`, `invalid_app`: the service's message names what was refused)
    uses the exception's message. An unexpected code is a 502: it is still the
    provider's or canopy's failure, never the caller's."""
    status, heading = _PROBLEMS.get(exc.code, (502, "Provider error"))
    detail = detail or {
        "not_configured": f"{title} is not set up for this workspace.",
        "wrong_account": f"That {title} account is not one {owner.lower() if owner == 'Your' else owner} "
                         "connection may use; sign in with the right account.",
        "not_connected": f"{owner} {title} is not connected.",
        "ambiguous": f"{owner} {title} has more than one account connected; disconnect all but one.",
        "insufficient_scope": f"{title} did not grant every permission canopy needs; connect again "
                              "and allow them all.",
        "provider_unavailable": f"{title} could not be reached; try again.",
        "no_vault_key": f"Set up your vault key first (Settings → Vault key): "
                        f"{owner.lower() if owner == 'Your' else owner} {title} connection is sealed to it.",
        "provider_error": f"{title} refused the request.",
    }.get(exc.code, str(exc))
    return ProblemError(status, heading, detail=detail, members={"error": exc.code or "provider_error", **members})


# Each refusal of a share, by its `error` code; the status comes with the refusal.
_WIRE_TITLES = {
    "bad_statement": "Invalid grant statement",
    "bad_signature": "Signature does not verify",
    "not_the_owner_key": "Not the owner's key",
    "statement_stale": "Already shared at that version",
    "not_an_admin": "Not an admin of that agent",
    "agent_outside_workspace": "Agent outside the workspace",
    "not_your_machine": "Not your computer",
    "statement_missing_owner_agent": "Statement must list the owner agent",
    "zk_value_changed": "Changed since you read it",
    "foreign_reader": "Sealed to someone the statement does not allow",
    "bad_writer_sig": "Invalid writer MACs",
    "revoking": "Being disconnected",
    "needs_share": _PROBLEMS["needs_share"][1],
    "bad_canopy_web": _PROBLEMS["bad_canopy_web"][1],
    "try_again": _PROBLEMS["try_again"][1],
    # A machine's material, lease and write-back (`tokens`, `proofs`).
    "device_proof_required": "Device proof required",
    "machine_not_enrolled": "Computer not enrolled",
    "not_on_my_computers": "Not used on this computer",
    "not_a_recipient": "Not a machine this connection reaches",
    "not_connected": "Not connected",
    "ambiguous": "More than one account connected",
    "needs_reconnect": "Reconnect required",
    "waiting": "Waiting for a copy",
    "lease_held": "Another machine is refreshing",
    "lease_lost": "Refresh lease lost",
    "zk_rewrap_required": "Drops a machine that still holds it",
    "rotation_mismatch": "Rotated refresh token missing",
    # A keeper's re-seal and a holder's revoke (`keeper`).
    "zk_not_a_holder": "Not a holder",
    "not_revoking": "Not being disconnected",
}


def problem_for_wire(exc: wire.WireError) -> ProblemError:
    """The response for a refused share (`sharing`), or a machine's refused material,
    lease or write-back (`tokens`, `proofs`)."""
    headers = {"WWW-Authenticate": 'DPoP algs="ES256"'} if exc.status == 401 else None
    return ProblemError(exc.status, _WIRE_TITLES.get(exc.code, "Refused"), detail=exc.detail,
                        members={"error": exc.code, **exc.members}, headers=headers)
