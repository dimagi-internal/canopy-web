"""The consent half of "Connect Google mailbox": which scopes, which account, the
consent link and its signed state. The token half — exchanging Google's code for a
refresh token — runs on one of the agent's runners (`google_mint.py`), never here.

The goal this serves, stated by Jonathan on 2026-09-05: *someone could plausibly
create a completely new agent without direct access to the cloud box or 1Password.*

WHY THE `canopy-web` CLIENT, NOT THE `canopy` ONE — the constraint that shapes
everything below. **A refresh token is minted FOR an OAuth client and works only
with that client.** The fleet's `canopy` client is a Desktop client: Google accepts
any loopback redirect for it and refuses an https one, so it cannot run a browser
flow that returns to canopy-web. canopy-web's own Web client accepts exactly one
https redirect (`callback_url`), so the consent runs on it, and the token declares
`client: canopy-web`. A box refreshes it with that client's id and secret from the
workspace's shared vault (`gog-oauth-client-web`) — the same item the runner
exchanges the code with.

The consent link carries a PKCE challenge whose verifier only the runner holds, so
the code Google sends back to canopy-web's callback cannot be redeemed here.
"""
from __future__ import annotations

import urllib.parse

from django.conf import settings
from django.core import signing

from .. import mailbox

# What to ASK Google for. The authority is what agents DECLARE they need, not
# what the incumbent tokens happen to carry — a distinction that cost a wrong
# reading on 2026-09-06.
#
# The live tokens (op://Agent-Echo, op://Agent-Eva) carry gmail, docs, drive,
# forms and sheets, so a first version requested exactly those, reasoning that a
# token unlike the fleet's is a new thing to debug. That inverted the evidence.
# Those tokens are a record of what was minted, not of what is required, and
# ace's config/agent.json says so in as many words:
#
#     gog_services: [gmail, calendar, drive, docs, slides, sheets, forms]
#     "ACE needs `slides` … and `calendar`, neither of which is in canopy's
#      fleet default."
#
# So the fleet default is KNOWN INSUFFICIENT for at least one agent already, and
# minting from it would hand ACE a token that authorizes cleanly and then fails
# its first slides call — the same shape of silent breakage this feature exists
# to end. Ask for the union instead.
#
# Over-requesting is close to free and under-requesting is not: an unused grant
# costs a line on the consent screen, while a missing one is a live agent failing
# mid-run, and re-minting means re-consenting. The runner derives the token's services
# from what Google actually GRANTED, so the token never overstates itself.
_BASE_SCOPES: tuple[str, ...] = (
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

# Declared by an agent, absent from every live token. Named separately so the
# reason survives: these are the two that ace's own config calls out as missing
# from the fleet default.
_DECLARED_BEYOND_THE_FLEET: tuple[str, ...] = (
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/presentations",
)

FLEET_SCOPES: tuple[str, ...] = _BASE_SCOPES + _DECLARED_BEYOND_THE_FLEET

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail"


# A person's OWN account, delegated to an agent (`google_mint.start_delegated`): ask for
# only what they chose, by gog's service names, never the fleet union above — it is
# someone's personal inbox, and every line on the consent screen is theirs to read.
_IDENTITY_SCOPES: tuple[str, ...] = ("openid", "email", "https://www.googleapis.com/auth/userinfo.email")
DELEGATED_SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    "gmail": ("https://www.googleapis.com/auth/gmail.modify",),
    "calendar": ("https://www.googleapis.com/auth/calendar",),
    "drive": ("https://www.googleapis.com/auth/drive",),
    "docs": ("https://www.googleapis.com/auth/documents",),
    "sheets": ("https://www.googleapis.com/auth/spreadsheets",),
}
DEFAULT_DELEGATED_SERVICES: tuple[str, ...] = ("gmail",)


def delegated_services(services) -> list[str]:
    """The services a delegated sign-in asks for, de-duplicated and in a fixed order;
    ValueError naming any this flow does not offer. None or empty = gmail."""
    wanted = [str(s).strip().lower() for s in (services or []) if str(s).strip()] or list(
        DEFAULT_DELEGATED_SERVICES)
    unknown = sorted(set(wanted) - set(DELEGATED_SERVICE_SCOPES))
    if unknown:
        raise ValueError(f"unknown Google service(s): {', '.join(unknown)} — choose from "
                         f"{', '.join(DELEGATED_SERVICE_SCOPES)}")
    return [s for s in DELEGATED_SERVICE_SCOPES if s in wanted]


def delegated_scopes(services) -> tuple[str, ...]:
    """The scopes for `delegated_services(services)`, with the identity scopes the runner
    needs to learn which account consented."""
    out = list(_IDENTITY_SCOPES)
    for s in delegated_services(services):
        out.extend(DELEGATED_SERVICE_SCOPES[s])
    return tuple(out)


def mailbox_kind(agent) -> str:
    """"google" when `agent.email` is a Google mailbox the runner reads with gog,
    "hosted" when canopy runs its mail (apps/mail, SES), "none" without one.

    Hosted means on a VERIFIED canopy domain (DKIM proves canopy holds its DNS), not
    merely a registered one: a workspace can add its domain before the MX cut-over, and
    until it verifies the address is still a Google mailbox that needs its Gmail scopes
    and the account check."""

    box = mailbox.describe(agent)
    if box["kind"] == "hosted":
        return "hosted" if box.get("verified") else "google"
    return box["kind"]


def scopes_for(agent) -> tuple[str, ...]:
    """What to ask Google for on THIS agent's behalf. An agent whose mail is
    canopy-hosted reads and sends it through canopy, so its Google login is for Drive,
    Docs, Sheets, Slides, Forms and Calendar only — asking it for Gmail too would grant
    access to an inbox nothing reads. Gmail stays when any of its addresses is still a
    Gmail mailbox the runner reads (ada@dimagi-ai.com beside ada@dimagi.ai)."""

    if mailbox_kind(agent) == "hosted" and not any(
            a["backend"] == "gmail" for a in mailbox.addresses(agent)):
        return tuple(s for s in FLEET_SCOPES if not s.startswith(_GMAIL_SCOPE))
    return FLEET_SCOPES


def expected_account(agent) -> str:
    """The Google account a consent must come from, or "" when any will do.

    `Agent.google_account` when set — the agent's Google login, named apart from its
    mailbox. Else `agent.email` when that is a Google mailbox: the token is only any use
    for that address, because the runner finds it by the address (`gog auth tokens
    list` keys are `<client>:<mailbox>`, mailbox_probe.py) and reads mail with
    `--account <agent.email>`. Else nothing names the account (a canopy-hosted address
    is not a Google account; an agent may have no address), so any will do. Mirrors
    `Agent.google_identity`, with hosted meaning live (`mailbox_kind`)."""
    if agent.google_account:
        return agent.google_account.strip().lower()
    return agent.email.strip().lower() if mailbox_kind(agent) == "google" else ""


# The name the token declares, and the name the box must have registered for it.
MINTING_CLIENT = "canopy-web"

AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"

STATE_SALT = "agents.google_oauth.state"
STATE_MAX_AGE = 900  # 15 minutes: long enough to sign in, short enough to matter.


def callback_url() -> str:
    """ONE redirect URI for the whole fleet.

    Google requires every redirect URI to be registered exactly, so a per-agent
    URI would mean a console edit per new agent — precisely the barrier this
    removes. The agent rides in the signed `state` instead.
    """
    return f"{settings.CANOPY_PUBLIC_BASE_URL.rstrip('/')}/api/oauth/google/callback"


def sign_state(*, agent_slug: str, user_pk, mint: str = "") -> str:
    """The agent, the person who started the sign-in, and the sign-in (`AgentGoogleMint`)
    the code belongs to."""
    return signing.dumps({"agent": agent_slug, "user": str(user_pk), "mint": mint}, salt=STATE_SALT)


def unsign_state(state: str) -> dict:
    return signing.loads(state, salt=STATE_SALT, max_age=STATE_MAX_AGE)


def authorize_url(*, client_id: str, state: str, code_challenge: str, login_hint: str = "",
                  scopes: tuple[str, ...] = FLEET_SCOPES, session: bool = False) -> str:
    """The consent link. `code_challenge` is the runner's (S256): the verifier never
    leaves the box, so the code that comes back here is useless to canopy-web. A
    `session` asks for an access token only (`access_type=online`: no refresh token, so it
    dies within the hour) and for only `scopes`, not every scope ever granted."""
    params = {
        "client_id": client_id,
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(scopes),
        # Both are required to get a refresh_token back. `access_type=offline`
        # asks for one; `prompt=consent` forces one to be RE-ISSUED even when the
        # account has approved this client before — without it a re-mint returns
        # only an access token and silently produces a token file with no
        # refresh_token in it, which imports fine and then cannot refresh.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if session:
        params.update(access_type="online", include_granted_scopes="false")
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTHORIZE_ENDPOINT}?{urllib.parse.urlencode(params)}"


def google_client_id() -> str:
    """canopy-web's own Web client — already configured, because it is what signs
    people in. Only its id is used here, for the consent link: the code is exchanged on a
    runner with the client's secret from the agent's vault, never here."""
    google = settings.SOCIALACCOUNT_PROVIDERS.get("google", {})
    app = google.get("APP", google)
    return app.get("client_id", "")
