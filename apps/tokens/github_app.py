"""The GitHub App user-to-server flow, and the token it yields.

Request-free service layer: takes a `user`, raises domain exceptions, and does
no HTTP-framework work — the views and (later) the agent-creation path both call
these, so the two cannot drift about what a valid grant is.

WHY A GITHUB APP AND NOT AN OAUTH APP. We need exactly `Administration: write`
to create a repository. The OAuth route would mean the `repo` scope, which is
read and write on every private repository the person can see — for Dimagi
staff, every private Dimagi repo, stored here, to serve a button pressed once
per agent. GitHub's own guidance says the same in general terms: Apps are
preferred because of fine-grained permissions, per-repository control, and
short-lived tokens. The August 2026 OAuth changes (token rotation, multiple
redirect URIs) closed the token-lifetime gap but explicitly did NOT change the
coarse scope model, so they do not change the answer.

WHAT THIS DELIBERATELY IS NOT. Not a credential a runner can hold, for three
separate reasons — the refresh token rotates so only one process may refresh it;
the installation is scoped to repos the app CREATED, which is not where agents
do their work; and a user token attributes every commit to that human, which
would make agent work indistinguishable from theirs in git history. Runner
GitHub access is an installation token, which is a different piece of work.
See `docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`.
"""
from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass

import requests
from django.conf import settings
from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret

from .models import GitHubConnection

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"

# Every GitHub call gets one, because a hung request here blocks a web worker.
HTTP_TIMEOUT = 15

# The session key the CSRF `state` is parked under between initiate and
# callback. Session-bound rather than a signed cookie so it is single-use by
# construction: the callback pops it.
STATE_SESSION_KEY = "github_oauth_state"


class GitHubNotConfigured(Exception):
    """No client id/secret on this deployment.

    A real state, not an error to hide: a fresh checkout and any deployment
    where the secret has not been set yet. Callers turn this into "GitHub is
    not set up on this deployment" rather than a 500, so `/settings` keeps
    working.
    """


class GitHubAuthError(Exception):
    """The grant cannot be used, and the user must press Connect again.

    Covers a rejected refresh, a revoked authorization, and a missing
    connection. Deliberately one type: they differ in cause and not in remedy.
    """


@dataclass(frozen=True)
class Installation:
    """One place this person has installed the app — an account or an org.

    `account_login` is what the owner dropdown shows and what
    `POST /orgs/{login}/repos` is addressed to; `is_org` decides which create
    endpoint to call.
    """

    installation_id: int
    account_login: str
    account_type: str  # "User" | "Organization"

    @property
    def is_org(self) -> bool:
        return self.account_type == "Organization"


def client_id() -> str:
    return (getattr(settings, "GITHUB_APP_CLIENT_ID", "") or "").strip()


def app_slug() -> str:
    """The app's slug, used to build the install link. Public, not a secret."""
    return (getattr(settings, "GITHUB_APP_SLUG", "") or "").strip()


def is_configured() -> bool:
    return bool(client_id() and (getattr(settings, "GITHUB_APP_CLIENT_SECRET", "") or "").strip())


def _require_configured() -> tuple[str, str]:
    cid = client_id()
    secret = (getattr(settings, "GITHUB_APP_CLIENT_SECRET", "") or "").strip()
    if not (cid and secret):
        raise GitHubNotConfigured("GITHUB_APP_CLIENT_ID / GITHUB_APP_CLIENT_SECRET are unset")
    return cid, secret


def install_url() -> str:
    """Where to send someone who wants the app on another account or org.

    Returns "" when the slug is unset, and the UI omits the link rather than
    rendering one that 404s.
    """
    slug = app_slug()
    return f"https://github.com/apps/{slug}/installations/new" if slug else ""


# ---- the authorization web flow ----------------------------------------------

def begin_authorization(session, *, redirect_uri: str) -> str:
    """Mint a `state`, park it in the session, and return the authorize URL.

    `state` is the CSRF defence for the leg we initiate: without it, an
    attacker can hand a victim a crafted callback URL and bind the attacker's
    GitHub account to the victim's canopy user. It is stored in the session (so
    it is bound to this browser) and popped by the callback (so it is
    single-use).
    """
    cid, _ = _require_configured()
    state = _secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state
    from urllib.parse import urlencode

    query = urlencode({"client_id": cid, "redirect_uri": redirect_uri, "state": state})
    return f"{GITHUB_AUTHORIZE_URL}?{query}"


def _exchange(payload: dict) -> dict:
    """POST to GitHub's token endpoint and return the parsed body.

    GitHub answers a REJECTED grant with HTTP 200 and an `error` key, so
    `raise_for_status()` alone would let a failure through as success. Both are
    checked.
    """
    resp = requests.post(
        GITHUB_TOKEN_URL,
        data=payload,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise GitHubAuthError(
            f"{body.get('error')}: {body.get('error_description', '')}".strip(": ")
        )
    return body


def _github_user(access_token: str) -> dict:
    resp = requests.get(
        f"{GITHUB_API}/user",
        headers=_auth_headers(access_token),
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 401:
        raise GitHubAuthError("GitHub rejected the access token")
    resp.raise_for_status()
    return resp.json()


def _auth_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _store(user, *, body: dict, gh_user: dict) -> GitHubConnection:
    """Persist a fresh grant. Encrypts the refresh token; drops the access one."""
    expires_in = body.get("refresh_token_expires_in")
    refresh_expires = (
        timezone.now() + timezone.timedelta(seconds=int(expires_in)) if expires_in else None
    )
    conn, _ = GitHubConnection.objects.update_or_create(
        user=user,
        defaults={
            "github_login": gh_user.get("login", ""),
            "github_user_id": int(gh_user.get("id") or 0),
            "refresh_token_enc": encrypt_secret(body.get("refresh_token", "")),
            "refresh_token_expires_at": refresh_expires,
            "refresh_failed_at": None,
        },
    )
    return conn


def complete_authorization(user, *, code: str, state: str | None, session) -> GitHubConnection:
    """Exchange `code` for tokens and store the grant against `user`.

    `state` is verified ONLY when the session holds one — i.e. only on the leg
    canopy initiated. GitHub also sends people here on paths it started itself
    (authorization during installation, and the "Redirect on update" bounce),
    and it echoes no `state` on those, so requiring one unconditionally would
    reject exactly the legitimate flows those settings exist to create.

    That is safe because the CSRF risk this guards is binding the WRONG GitHub
    account to a canopy user, and it is still covered: a session with a pending
    state must match it, and a session without one had no in-flight canopy
    request to hijack. The stored state is popped either way, so it can never
    be replayed.
    """
    expected = session.pop(STATE_SESSION_KEY, None)
    if expected and state != expected:
        raise GitHubAuthError("state mismatch — start the connection again from Settings")

    cid, secret = _require_configured()
    body = _exchange({"client_id": cid, "client_secret": secret, "code": code})
    access_token = body.get("access_token") or ""
    if not access_token:
        raise GitHubAuthError("GitHub returned no access token")
    if not body.get("refresh_token"):
        # Only happens if "Expire user authorization tokens" is OFF on the app.
        # Refuse rather than silently storing a credential that never expires:
        # the whole design treats the access token as disposable, and a
        # non-expiring token with `Administration: write` is a different risk
        # than the one that was signed off.
        raise GitHubAuthError(
            "GitHub issued no refresh token — the app must have "
            "'Expire user authorization tokens' enabled"
        )
    return _store(user, body=body, gh_user=_github_user(access_token))


# ---- using the grant ---------------------------------------------------------

def access_token_for(user) -> str:
    """A usable user access token, refreshed on the spot.

    Refresh-on-use rather than refresh-on-schedule: the access token lives 8
    hours and this is called a handful of times per user ever, so a background
    refresher would be machinery for nothing — and one that fell behind would
    fail at exactly the moment someone pressed a button.

    GitHub rotates the refresh token, so the new one is stored before the
    caller does anything with the access token. A rejection stamps
    `refresh_failed_at`, which is what turns an opaque mid-operation 401 into
    "reconnect GitHub" on `/settings`.
    """
    conn = GitHubConnection.objects.filter(user=user).first()
    if conn is None:
        raise GitHubAuthError("no GitHub connection — connect GitHub in Settings")
    if conn.needs_reconnect:
        raise GitHubAuthError("your GitHub connection expired — reconnect it in Settings")

    cid, secret = _require_configured()
    try:
        body = _exchange({
            "client_id": cid,
            "client_secret": secret,
            "grant_type": "refresh_token",
            "refresh_token": decrypt_secret(conn.refresh_token_enc),
        })
    except GitHubAuthError:
        conn.refresh_failed_at = timezone.now()
        conn.save(update_fields=["refresh_failed_at", "updated_at"])
        raise

    new_refresh = body.get("refresh_token") or ""
    expires_in = body.get("refresh_token_expires_in")
    conn.refresh_token_enc = encrypt_secret(new_refresh) if new_refresh else conn.refresh_token_enc
    if expires_in:
        conn.refresh_token_expires_at = timezone.now() + timezone.timedelta(seconds=int(expires_in))
    conn.refresh_failed_at = None
    conn.last_used_at = timezone.now()
    conn.save(update_fields=[
        "refresh_token_enc", "refresh_token_expires_at", "refresh_failed_at",
        "last_used_at", "updated_at",
    ])

    token = body.get("access_token") or ""
    if not token:
        raise GitHubAuthError("GitHub returned no access token on refresh")
    return token


def list_installations(user) -> list[Installation]:
    """Where this person has actually installed the app.

    This is the owner dropdown's only source. Asking someone to TYPE an owner
    invites a name the app cannot write to, and hardcoding an org would break
    the "not just Dimagi users" requirement outright.

    Deliberately re-queried rather than cached, and never taken from a URL: the
    "Redirect on update" callback carries an `installation_id`, and GitHub warns
    that "bad actors can hit this URL with a spoofed `installation_id`". Reading
    the list with the user's own token means a spoofed id cannot make canopy
    believe anything — it just causes a redundant lookup.
    """
    token = access_token_for(user)
    resp = requests.get(
        f"{GITHUB_API}/user/installations",
        headers=_auth_headers(token),
        params={"per_page": 100},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code == 401:
        raise GitHubAuthError("GitHub rejected the access token — reconnect GitHub in Settings")
    resp.raise_for_status()
    out: list[Installation] = []
    for row in resp.json().get("installations", []):
        account = row.get("account") or {}
        login = account.get("login") or ""
        if not login:
            continue
        out.append(Installation(
            installation_id=int(row.get("id") or 0),
            account_login=login,
            account_type=account.get("type") or "User",
        ))
    return out


def disconnect(user) -> bool:
    """Forget the grant. Returns whether there was one.

    Local only, deliberately. Revoking canopy's own copy is what a user asking
    to disconnect is asking for, and it is the half we can guarantee; the
    authorization itself is revoked from GitHub's own settings, which the UI
    links to. Pretending to revoke upstream and silently failing would be
    worse than saying which half this does.
    """
    deleted, _ = GitHubConnection.objects.filter(user=user).delete()
    return bool(deleted)
