"""What a minted browser session looks like to the rest of the app (`is_machine`).

The minting view is `apps/tokens/mint_session.py`.

Minting a browser session from a token:

POST /api/debug/mint-session/ turns an authenticated caller — in practice a PAT
— into a short-lived Django session cookie. Its one consumer is the macOS
menubar app, which trades the runner's PAT for a cookie so its web view opens
/supervisor already signed in; a bearer header cannot log a web view in.

It used to be a Settings button for handing a cookie to an AI assistant. That
button is gone (2026-09-25): an assistant working the API uses a short-lived PAT.

A MINTED SESSION IS STILL A MACHINE. The few "canopy web app only" decisions
(transfer an agent's owner, change its admins) refuse a bearer token that authenticated the request
(`request.auth_method` set),
so a leaked token cannot take over an agent — and before this, any PAT could
walk straight past that by minting a cookie first. Every minted session carries
`DEBUG_SESSION_MARKER`, and `is_machine(request)` treats it exactly like a
bearer, so those gates hold for a person signed in through Google and nobody else.
"""
DEBUG_SESSION_MARKER = "_canopy_debug_session"


def is_machine(request) -> bool:
    """True when a token, not a person signed in through the browser, is behind
    this request: a bearer token that authenticated it, an MCP tool call (dispatched
    in-process, so it carries no header), or a session minted from a token."""
    # An Authorization header counts only when a token actually authenticated
    # the request: every bearer route in apps/tokens/middleware.py stamps
    # `auth_method`. A signed-in cookie user skips the PAT lookup, so a junk
    # `Authorization: Bearer x` sent with a stolen cookie leaves it unset and
    # must not read as "a token" (it would skip every passkey step-up).
    if getattr(request, "auth_method", None) or getattr(request, "via_mcp", False):
        return True
    session = getattr(request, "session", None)
    try:
        return bool(session is not None and session.get(DEBUG_SESSION_MARKER))
    except Exception:  # noqa: BLE001 — an unreadable session is not a person
        return True
