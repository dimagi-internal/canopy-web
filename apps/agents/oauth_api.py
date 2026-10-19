"""The two routes behind the "Connect Google mailbox" button.

Split from api.py because the callback CANNOT live under `/agents/{slug}/` — one
redirect URI has to serve every agent (see google_oauth.callback_url), so the
agent travels in signed state and the callback is mounted at a fixed path.

What lands at the end is an ordinary AgentCredential named `gog-token`, holding
exactly the JSON that `gog auth tokens import` accepts. Nothing downstream learns
that a browser was involved: the runner resolves it, writes it to a file, and
imports it the same way it imports one a human minted at a terminal.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth

from . import google_oauth, services
from .schemas import GoogleMintStartOut

router = Router(auth=session_auth, tags=["agents"])

# The credential slot the fleet already names in every runtime.yaml and every
# 1Password vault. Minting must land in the SAME slot a hand-minted token does,
# or the box would have two sources of truth for one mailbox.
GOG_TOKEN_REF = "gog-token"


def _agent_for_mint(request: HttpRequest, slug: str):
    """An agent whose mailbox credential the caller may REPLACE — its owner or
    an admin, the same tier as `PUT /{slug}/credentials`. The mint writes the
    `gog-token` credential, so membership alone let a viewer attach their own
    Google account as the agent's mailbox."""
    from .api import _agent_for_admin

    return _agent_for_admin(request, slug)


@router.get("/{slug}/google/authorize", response=GoogleMintStartOut,
            summary="Start the Google mint for this agent's mailbox")
def start_google_mint(request: HttpRequest, slug: str, login_hint: str = "") -> GoogleMintStartOut:
    """Returns the URL rather than redirecting, so the caller opens it itself.

    A 302 out of an XHR is invisible — the browser follows it, the fetch resolves
    on Google's HTML, and nothing happens on screen. Handing back the URL lets the
    page do a top-level navigation, which is the only thing that can show a
    consent screen.
    """
    agent = _agent_for_mint(request, slug)
    client_id, _ = google_oauth.google_client_credentials()
    if not client_id:
        raise HttpError(503, "this deployment has no Google OAuth client configured")
    state = google_oauth.sign_state(agent_slug=agent.slug, user_pk=request.user.pk)
    # Default the account picker to the account the callback will insist on
    # (`expected_account`: its `google_account`, else its mailbox when that is Google),
    # else the fleet's convention as a guess — a hosted-mail agent commonly signs in to
    # Google as <slug>@dimagi-ai.com. A hint, not the check: the account can still be
    # changed on Google's side.
    hint = login_hint or google_oauth.expected_account(agent) or f"{agent.slug}@dimagi-ai.com"
    return GoogleMintStartOut(
        url=google_oauth.authorize_url(client_id=client_id, state=state, login_hint=hint,
                                       scopes=google_oauth.scopes_for(agent))
    )


oauth_router = Router(auth=session_auth, tags=["agents"])


@oauth_router.get("/google/callback", summary="Google returns here; the token is stored")
def google_callback(request: HttpRequest, code: str = "", state: str = "", error: str = ""):
    from django.core import signing

    def done(agent, status: str, **extra: str):
        # ROOT-RELATIVE IS WRONG HERE. This deployment is served under a path
        # prefix (/canopy), so "/w/…" lands outside the app entirely — the mint
        # succeeded, the token was stored, and the operator got a Resolver404
        # that reads exactly like a failure. Reported by Jonathan on the first
        # real sign-in, 2026-09-07.
        #
        # CANOPY_PUBLIC_BASE_URL already carries the prefix and is already the
        # authority for the OAuth redirect_uri, so the round trip starts and ends
        # against the same base rather than two derivations that can disagree.
        root = settings.CANOPY_PUBLIC_BASE_URL.rstrip("/")
        # Credentials is a section of the agent's SETTINGS page (it moved off
        # Overview with the rest of the agent's configuration, 2026-09-23); the
        # fragment lands the person on it rather than at the top of the page.
        # `/credentials` still redirects here too, so an older link survives.
        from urllib.parse import urlencode

        query = urlencode({"google": status, **extra})
        if agent is None:
            return HttpResponseRedirect(f"{root}/?{query}")
        path = f"/w/{agent.workspace.slug}/agents/{agent.slug}/settings"
        return HttpResponseRedirect(f"{root}{path}?{query}#credentials")

    if not state:
        raise HttpError(400, "missing state")
    try:
        claims = google_oauth.unsign_state(state)
    except signing.BadSignature as exc:
        # Covers both tampering and expiry. Either way the only safe move is to
        # start over — a code redeemed under a state we cannot vouch for could
        # attach someone else's mailbox to this agent.
        raise HttpError(400, "the sign-in link expired or was altered — start again") from exc

    if str(claims.get("user")) != str(request.user.pk):
        raise HttpError(403, "this sign-in was started by a different user")

    agent = _agent_for_mint(request, claims["agent"])
    if error or not code:
        return done(agent, "denied")

    client_id, client_secret = google_oauth.google_client_credentials()
    try:
        tokens = google_oauth.exchange_code(code=code, client_id=client_id, client_secret=client_secret)
    except Exception:  # noqa: BLE001 - the browser gets a status, the operator retries
        return done(agent, "exchange-failed")

    refresh = tokens.get("refresh_token", "")
    if not refresh:
        # Google withholds it when the account already consented and `prompt` was
        # not forced. Storing what we got would produce a token that imports and
        # then cannot refresh — worse than failing, because it looks provisioned.
        return done(agent, "no-refresh-token")

    email = google_oauth.email_from_id_token(tokens["id_token"]) if tokens.get("id_token") else ""
    if not email:
        return done(agent, "no-email")
    expected = google_oauth.expected_account(agent)
    if expected and email.strip().lower() != expected:
        # Refused before anything is stored: a token for another account can never read
        # this agent's mailbox, and stored it would read as "connected". The page names
        # the account Google signed in as, so the person can tell which one they used.
        return done(agent, "wrong-account", account=email, expected=expected)

    token = google_oauth.build_gog_token(
        email=email,
        refresh_token=refresh,
        granted_scopes=tokens.get("scope", "").split(),
    )
    import json as _json

    from .zk import ZkError

    try:
        # Minted here: a zero-knowledge agent gets it sealed to its devices and
        # members (`zks1`), never stored where canopy-web could read it.
        services.set_agent_credentials(agent, {GOG_TOKEN_REF: _json.dumps(token)}, user=request.user,
                                       minted=True)
    except ZkError:
        return done(agent, "zk-no-recipients")
    return done(agent, "ok")
