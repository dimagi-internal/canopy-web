"""Django Ninja v2 router for the /api/tokens/ surface."""
from __future__ import annotations

import requests
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status

from apps.api.auth import session_auth
from apps.api.errors import TYPE_NOT_FOUND, ProblemError

from . import github_app
from .models import GitHubConnection, PersonalToken
from .schemas import (
    GitHubConnectionOut,
    GitHubInstallationOut,
    PersonalTokenCreatedOut,
    PersonalTokenCreateIn,
    PersonalTokenOut,
)

router = Router(auth=session_auth, tags=["tokens"])


def _serialize(token: PersonalToken) -> dict:
    return {
        "id": token.pk,
        "label": token.label,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
        "expires_at": token.expires_at,
    }


@router.get("/", response=list[PersonalTokenOut], summary="List my tokens")
def list_tokens(request: HttpRequest) -> list[PersonalTokenOut]:
    qs = PersonalToken.objects.filter(user=request.user).order_by("-created_at")
    return [PersonalTokenOut.model_validate(_serialize(t)) for t in qs]


@router.post("/", response={201: PersonalTokenCreatedOut}, summary="Mint a token")
def create_token(request: HttpRequest, payload: PersonalTokenCreateIn) -> Status:
    raw, token = PersonalToken.create_for_user(
        user=request.user, label=payload.label, ttl_days=payload.ttl_days
    )
    body = _serialize(token) | {"raw": raw}
    return Status(201, PersonalTokenCreatedOut.model_validate(body))


@router.delete("/{pk}/", response={204: None}, summary="Revoke a token (mine only)")
def revoke_token(request: HttpRequest, pk: int) -> Status:
    token = PersonalToken.objects.filter(pk=pk, user=request.user).first()
    if token is None:
        raise ProblemError(404, "Token not found", type_=TYPE_NOT_FOUND)
    if token.revoked_at is None:
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
    return Status(204, None)


# ---- GitHub App connection (per user) ----------------------------------------
# The browser legs of this flow are bare Django views (redirects + session) at
# /auth/github/start/ and /auth/github/callback/ — see github_views.py. These
# three routes are the JSON half that the Settings page and the create-agent
# form read.


@router.get("/github", response=GitHubConnectionOut, summary="My GitHub connection")
def github_connection(request: HttpRequest) -> GitHubConnectionOut:
    """Whether this deployment has GitHub set up, and whether I am connected.

    Never touches GitHub. It is read on every visit to `/settings`, and a
    network call there would make an unrelated page slow — and fail — because
    of a third party. Staleness is not a risk: the only thing it could be stale
    about is `needs_reconnect`, which the next real operation stamps anyway.
    """
    conn = GitHubConnection.objects.filter(user=request.user).first()
    return GitHubConnectionOut.model_validate({
        "configured": github_app.is_configured(),
        "connected": conn is not None,
        "github_login": conn.github_login if conn else "",
        "needs_reconnect": conn.needs_reconnect if conn else False,
        "install_url": github_app.install_url(),
    })


@router.delete("/github", response={204: None}, summary="Disconnect GitHub (mine only)")
def github_disconnect(request: HttpRequest) -> Status:
    """Forget my grant.

    Scoped to `request.user` with no id in the path, so there is no object to
    address and therefore nothing to get wrong about whose grant is being
    deleted. Idempotent: disconnecting when not connected is a 204, because the
    caller's intent is already satisfied.

    Deletes canopy's copy only — revoking the authorization itself happens in
    GitHub's own settings, which the UI links to. Claiming to revoke upstream
    and silently failing would be worse than saying which half this does.
    """
    github_app.disconnect(request.user)
    return Status(204, None)


@router.get("/github/installations", response=list[GitHubInstallationOut],
            summary="Where I have installed the app (the owner picker)")
def github_installations(request: HttpRequest) -> list[GitHubInstallationOut]:
    """The accounts and orgs the caller can have a repo created in.

    This is what the create-agent form's owner dropdown renders. It is a live
    read, unlike `GET /github` — the whole point is that it reflects an install
    the user may have just done in another tab, and a cached list would offer
    an owner that no longer works or omit the one they just added.

    A 409 (not 500) when the grant is unusable, because the remedy is a user
    action — press Connect — rather than anything a retry would fix.
    """
    try:
        rows = github_app.list_installations(request.user)
    except github_app.GitHubNotConfigured:
        raise ProblemError(
            409, "GitHub is not configured on this deployment",
            detail="No GitHub App credentials are set. Ask an administrator.",
        ) from None
    except github_app.GitHubAuthError as exc:
        raise ProblemError(
            409, "GitHub needs reconnecting", detail=str(exc),
        ) from None
    except requests.RequestException as exc:
        raise ProblemError(
            502, "Could not reach GitHub", detail=str(exc),
        ) from None
    return [
        GitHubInstallationOut.model_validate({
            "installation_id": r.installation_id,
            "account_login": r.account_login,
            "account_type": r.account_type,
            "is_org": r.is_org,
            "repository_selection": r.repository_selection,
            "repositories": list(r.repositories),
            "repository_count": r.repository_count,
        })
        for r in rows
    ]


# --- On-behalf-of: canopy's public key -------------------------------------
#
# Published so a HOST can verify the assertions canopy signs about who its
# agent is currently answering (`apps/tokens/onbehalf.py`). Public by
# necessity and by nature: it is a public key, a verifier must be able to fetch
# it before it trusts anything, and JWKS is the shape every JWT library already
# reads. `auth=None` plus an entry in the login middleware's allowlist, like
# the inbound `contact-token` route it is the mirror of.
#
# An unconfigured deployment answers with an EMPTY key set rather than an
# error: "this canopy signs nothing" is a real state, and a host polling the
# endpoint should read it as "no keys", not as an outage.
onbehalf_router = Router(auth=None, tags=["tokens"])


@onbehalf_router.get("/on-behalf-of/jwks", response=dict, auth=None,
                     summary="Public keys for canopy's on-behalf-of assertions")
def onbehalf_jwks(request: HttpRequest) -> dict:
    """The public keys that verify canopy's on-behalf-of assertions.

    A canopy agent answering someone on your site can attach a short assertion
    saying who it is answering: `iss` is this canopy, `aud` is your site's
    registered name, `sub` is YOUR id for that person, and `act.sub` is the
    agent. Verify it against these keys and act as that person, for that call
    only — `exp` is 120 seconds and `jti` is single-use if you track it.
    """
    from . import onbehalf

    if not onbehalf.configured():
        return {"keys": []}
    return {"keys": onbehalf.published_jwks()}
