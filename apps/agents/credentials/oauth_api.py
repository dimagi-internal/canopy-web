"""The routes behind the "Connect Google mailbox" button.

The sign-in finishes on one of the agent's runners, so canopy-web never holds the
token (`google_mint.py`): these routes start it, report it, and receive Google's
return trip. The callback CANNOT live under `/agents/{slug}/` — one redirect URI has
to serve every agent (see google_oauth.callback_url), so the agent and the sign-in
travel in signed state and the callback is mounted at a fixed path.

What lands at the end is an ordinary AgentCredential named `gog-token`, holding
ciphertext of exactly the JSON that `gog auth tokens import` accepts. Nothing
downstream learns that a browser was involved.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest, HttpResponseRedirect
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate

from ..gates import agent_for_admin
from ..schemas import GoogleMintOut, GoogleMintStartIn
from . import google_mint, google_oauth

router = Router(auth=session_auth, tags=["agents"])

# The credential slot the fleet already names in every agent's declaration.
GOG_TOKEN_REF = google_mint.GOG_TOKEN_REF


def mint_out(mint) -> GoogleMintOut:
    return GoogleMintOut(
        id=mint.id, status=mint.status, outcome=mint.outcome, authorize_url=mint.authorize_url,
        runner=mint.runner.name if mint.runner_id else "", account=mint.account,
        expected_account=google_oauth.expected_account(mint.agent), detail=mint.detail,
        created_at=mint.created_at, updated_at=mint.updated_at)


def mint_problem(exc: google_mint.MintRefused) -> ProblemError:
    return ProblemError(exc.status, "Google sign-in", detail=exc.detail, members={"error": exc.code},
                        type_=TYPE_CONFLICT if exc.status == 409 else TYPE_VALIDATION)


@router.post("/{slug}/google/mint", response=GoogleMintOut,
             summary="Start connecting this agent's Google account (finished on its runner)")
# the mint's token is exchanged and encrypted on the runner: nothing secret here
@gate("agent-admin", kind="action")
def start_google_mint(request: HttpRequest, slug: str, payload: GoogleMintStartIn | None = None) -> GoogleMintOut:
    """For the agent's owner or an admin (the mint replaces its `gog-token`). Hands
    the sign-in to one of the agent's online runners; 409 `no_mint_runner` when none
    is online — there is no fallback, because canopy never holds the token. Poll
    `get_google_mint` until `authorize_url` is set (a few seconds), then open it with
    a top-level navigation: a redirect followed inside a fetch shows nothing."""
    agent = agent_for_admin(request, slug)
    try:
        mint = google_mint.start(agent, user=request.user, login_hint=payload.login_hint if payload else "")
    except google_mint.MintRefused as exc:
        raise mint_problem(exc) from exc
    return mint_out(mint)


@router.get("/{slug}/google/mint/{mint_id}", response=GoogleMintOut,
            summary="How a Google sign-in for this agent is going")
@gate("agent-admin")
def get_google_mint(request: HttpRequest, slug: str, mint_id: uuid.UUID) -> GoogleMintOut:
    """`status` and, once it ends, `outcome` — never a code or a token."""
    agent = agent_for_admin(request, slug)
    mint = google_mint.get(agent, mint_id)
    if mint is None:
        raise HttpError(404, "no such sign-in")
    return mint_out(mint)


oauth_router = Router(auth=session_auth, tags=["agents"])


@oauth_router.get("/google/callback", summary="Google returns here; a runner finishes the sign-in")
@gate("signed-link", "agent-admin")  # signed state must name the caller
def google_callback(request: HttpRequest, code: str = "", state: str = "", error: str = ""):
    from django.core import signing

    from ..models import AgentGoogleMint

    def back(agent, status: str, mint=None):
        # The deployment's public base (it may carry a path prefix): the same base the
        # redirect_uri is built from, so the round trip starts and ends on one address.
        root = settings.CANOPY_PUBLIC_BASE_URL.rstrip("/")
        query = urlencode({"google": status, **({"mint": str(mint.id)} if mint else {})})
        if agent is None:
            return HttpResponseRedirect(f"{root}/?{query}")
        path = f"/w/{agent.workspace.slug}/agents/{agent.slug}/settings"
        return HttpResponseRedirect(f"{root}{path}?{query}#credentials")

    if not state:
        raise HttpError(400, "missing state")
    try:
        claims = google_oauth.unsign_state(state)
    except signing.BadSignature as exc:
        # Tampering or expiry: a code redeemed under a state we cannot vouch for could
        # attach someone else's mailbox to this agent.
        raise HttpError(400, "the sign-in link expired or was altered — start again") from exc
    if str(claims.get("user")) != str(request.user.pk):
        raise HttpError(403, "this sign-in was started by a different user")

    agent = agent_for_admin(request, claims["agent"])
    mint = AgentGoogleMint.objects.filter(pk=claims.get("mint") or None, agent=agent).first() \
        if claims.get("mint") else None
    if mint is None:
        return back(agent, "expired")
    mint = google_mint.receive_code(mint, code=code, error=error)
    if mint.status == AgentGoogleMint.COMPLETING:
        return back(agent, "pending", mint)
    return back(agent, mint.outcome or "expired", mint)
