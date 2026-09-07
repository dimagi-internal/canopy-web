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


def _agent_or_404(request: HttpRequest, slug: str):
    from .api import _get_agent_or_404

    return _get_agent_or_404(request, slug)


@router.get("/{slug}/google/authorize", response=GoogleMintStartOut,
            summary="Start the Google mint for this agent's mailbox")
def start_google_mint(request: HttpRequest, slug: str, login_hint: str = "") -> GoogleMintStartOut:
    """Returns the URL rather than redirecting, so the caller opens it itself.

    A 302 out of an XHR is invisible — the browser follows it, the fetch resolves
    on Google's HTML, and nothing happens on screen. Handing back the URL lets the
    page do a top-level navigation, which is the only thing that can show a
    consent screen.
    """
    agent = _agent_or_404(request, slug)
    client_id, _ = google_oauth.google_client_credentials()
    if not client_id:
        raise HttpError(503, "this deployment has no Google OAuth client configured")
    state = google_oauth.sign_state(agent_slug=agent.slug, user_pk=request.user.pk)
    # Default the account picker to the agent's own mailbox. It is a HINT, not a
    # constraint — whoever is signing in can still choose another account, and the
    # token records whichever mailbox actually consented.
    hint = login_hint or f"{agent.slug}@dimagi-ai.com"
    return GoogleMintStartOut(
        url=google_oauth.authorize_url(client_id=client_id, state=state, login_hint=hint)
    )


oauth_router = Router(auth=session_auth, tags=["agents"])


@oauth_router.get("/google/callback", summary="Google returns here; the token is stored")
def google_callback(request: HttpRequest, code: str = "", state: str = "", error: str = ""):
    from django.core import signing

    def done(agent, status: str):
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
        path = f"/w/{agent.workspace.slug}/agents/{agent.slug}/credentials" if agent else "/"
        return HttpResponseRedirect(f"{root}{path}?google={status}")

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

    agent = _agent_or_404(request, claims["agent"])
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

    token = google_oauth.build_gog_token(
        email=email,
        refresh_token=refresh,
        granted_scopes=tokens.get("scope", "").split(),
    )
    import json as _json

    services.set_agent_credentials(agent, {GOG_TOKEN_REF: _json.dumps(token)}, user=request.user)
    return done(agent, "ok")
