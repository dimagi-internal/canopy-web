"""Mint a gog-importable Google token from a browser.

The goal this serves, stated by Jonathan on 2026-09-05: *someone could plausibly
create a completely new agent without direct access to the cloud box or 1Password.*
A mailbox was the last thing that could not be provisioned that way — minting a
gog token meant a terminal, `gog auth login`, a loopback listener, and then a
hand-written 1Password item. This turns it into a button.

WHY THIS CANNOT USE THE `canopy` CLIENT — the constraint that shapes everything
below, and the one that cost a wrong deploy on 2026-09-05. **A refresh token is
minted FOR an OAuth client and works only with that client.** So the client that
runs the browser flow is the client the resulting token is bound to, forever.

And the fleet's `canopy` client CANNOT run a browser flow. Measured against
Google's authorize endpoint on 2026-09-06:

    canopy  (…l3iom9j3…)  http://localhost:59999/  -> accepted (login page)
    canopy  (…l3iom9j3…)  https://…/callback       -> redirect_uri_mismatch
    canopy-web (…osh2en…) http://localhost:59999/  -> redirect_uri_mismatch
    canopy-web (…osh2en…) https://…/callback/      -> accepted (login page)

Accepting an arbitrary unregistered loopback port is the signature of a **Desktop**
client; refusing loopback while accepting one exact https URI is the signature of
a **Web application** client. Google does not permit an https redirect on a Desktop
client at all, so no amount of console configuration makes `canopy` mint from a
browser. The web flow therefore runs on canopy-web's own Web client — already in
the same GCP project, already carrying these scopes on the shared consent screen,
and already present in this process's settings because it is what signs people in.

The token consequently declares `client: canopy-web`, and the box must have that
client registered to refresh it. `bootstrap_agents.sh` reads the client name from
the token itself, so it picks this up with no per-agent configuration.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from django.conf import settings
from django.core import signing

# The scope set is READ FROM THE LIVE FLEET, not designed here: echo's and eva's
# working tokens carry exactly these. An earlier draft added calendar and slides
# on the assumption they were wanted; no token in the fleet has ever had them,
# and a minted token that differs from the fleet's is a new thing to debug rather
# than a replacement for the old one.
FLEET_SCOPES: tuple[str, ...] = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://www.googleapis.com/auth/spreadsheets",
)

# gog's short service names, keyed by the scope-path prefix that implies them.
# Ordered longest-prefix-first is unnecessary here because no key is a prefix of
# another, but the map is checked by test rather than trusted.
_SERVICE_BY_SCOPE_PREFIX: tuple[tuple[str, str], ...] = (
    ("https://www.googleapis.com/auth/gmail", "gmail"),
    ("https://www.googleapis.com/auth/documents", "docs"),
    ("https://www.googleapis.com/auth/drive", "drive"),
    ("https://www.googleapis.com/auth/forms", "forms"),
    ("https://www.googleapis.com/auth/spreadsheets", "sheets"),
    ("https://www.googleapis.com/auth/presentations", "slides"),
    ("https://www.googleapis.com/auth/calendar", "calendar"),
)

# The name the token declares, and the name the box must have registered for it.
MINTING_CLIENT = "canopy-web"

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

STATE_SALT = "agents.google_oauth.state"
STATE_MAX_AGE = 900  # 15 minutes: long enough to sign in, short enough to matter.


def services_for(scopes) -> list[str]:
    """gog's `services` list, derived from the scopes Google actually GRANTED.

    Derived, never declared: a user can uncheck a scope on the consent screen, and
    a token that claims a service it was not granted fails at call time instead of
    at mint time — which is how a mailbox stays broken for four months.
    """
    out = set()
    for scope in scopes:
        for prefix, service in _SERVICE_BY_SCOPE_PREFIX:
            if scope == prefix or scope.startswith(prefix + "."):
                out.add(service)
    return sorted(out)


def build_gog_token(
    *, email: str, refresh_token: str, granted_scopes, client: str = MINTING_CLIENT, now=None
) -> dict:
    """The exact on-disk shape `gog auth tokens import` accepts.

    Five keys, no more: matched field-for-field against the live tokens in
    op://Agent-Echo, op://Agent-Eva and op://Agent-Ace rather than reconstructed
    from gog's source, because the importer is what has to accept it.
    """
    scopes = sorted(set(granted_scopes))
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "email": email,
        "client": client,
        "services": services_for(scopes),
        "scopes": scopes,
        "created_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "refresh_token": refresh_token,
    }


def callback_url() -> str:
    """ONE redirect URI for the whole fleet.

    Google requires every redirect URI to be registered exactly, so a per-agent
    URI would mean a console edit per new agent — precisely the barrier this
    removes. The agent rides in the signed `state` instead.
    """
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}/api/oauth/google/callback"


def sign_state(*, agent_slug: str, user_pk) -> str:
    return signing.dumps({"agent": agent_slug, "user": str(user_pk)}, salt=STATE_SALT)


def unsign_state(state: str) -> dict:
    return signing.loads(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)


def authorize_url(*, client_id: str, state: str, login_hint: str = "") -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(FLEET_SCOPES),
        # Both are required to get a refresh_token back. `access_type=offline`
        # asks for one; `prompt=consent` forces one to be RE-ISSUED even when the
        # account has approved this client before — without it a re-mint returns
        # only an access token and silently produces a token file with no
        # refresh_token in it, which imports fine and then cannot refresh.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _post_form(url: str, data: dict, *, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310 - fixed https endpoint
        return json.loads(res.read().decode())


def email_from_id_token(id_token: str) -> str:
    """Read the mailbox out of the id_token's payload.

    No signature check on purpose, and it is safe here for a specific reason: this
    JWT did not come from the browser, it came back over TLS from Google's token
    endpoint in a request we originated. There is no untrusted party in the path
    to forge it. (Reading an id_token that ARRIVED from a client would need full
    verification — that is a different situation, and this is not it.)
    """
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    import base64

    return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")


def exchange_code(*, code: str, client_id: str, client_secret: str) -> dict:
    """Trade the one-time code for a refresh token. Returns Google's raw response."""
    return _post_form(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": callback_url(),
            "grant_type": "authorization_code",
        },
    )


def google_client_credentials() -> tuple[str, str]:
    """canopy-web's own Web client — already configured, because it is what signs
    people in. Nothing new to stage anywhere: that was the point."""
    google = settings.SOCIALACCOUNT_PROVIDERS.get("google", {})
    app = google.get("APP", google)
    return app.get("client_id", ""), app.get("secret", "")
