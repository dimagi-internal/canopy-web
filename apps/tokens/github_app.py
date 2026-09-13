"""One per-user GitHub grant, for everything canopy and its agents do on GitHub.

Request-free service layer: takes a `user`, raises domain exceptions, and does
no HTTP-framework work — the views, the agent-creation path, and (next) the
runner token endpoint all call these, so they cannot drift about what a valid
grant is.

WHY A GITHUB APP AND NOT AN OAUTH APP. An OAuth App has only coarse scopes: the
narrowest thing that can push to a repository is `repo`, which is read AND write
on every private repository that person can see — for Dimagi staff, every
private Dimagi repo, held here, to serve an agent pushing to one. A GitHub App
grants per-repository access the USER chooses, at per-action granularity.
GitHub's own guidance says the same generally: Apps are preferred for
fine-grained permissions, per-repository control, and short-lived tokens. The
August 2026 OAuth changes (token rotation, multiple redirect URIs) closed the
token-lifetime gap but explicitly did NOT touch the coarse scope model, so they
do not change the answer.

WHY THERE IS NO `Administration: write`, AND WHY THAT COST A CLICK. Creating a
repository requires it — there is no narrow path, including "create from a
template", which needs `Administration: write` AND `Contents: read` together.
And a user access token CANNOT be down-scoped: unlike an installation token,
there is no way to mint a reduced one, so it always carries the app's full
permission set. Holding `Administration: write` therefore meant every runner
token could DELETE repositories, handed to agents running Claude Code with
permissions bypassed — the exact class of accident to design out.

So canopy does not create repositories. The user creates a blank repo and hands
it over, and this app holds only what pushing and collaborating needs
(`Contents`, `Workflows`, `Pull requests`, `Issues`). Nothing canopy or any
agent holds can delete a repository or change its settings.

WHICH IS WHY THIS *IS* THE RUNNER CREDENTIAL, not a thing to keep away from
runners. A user access token is the only GitHub credential that is inherently
per-person — an installation token is per-INSTALLATION, so everyone working in
one org would share it, which is the shared-account model this replaces. Agent
commits are attributed to the human the work is on behalf of, which is the
point. Runners never hold a durable credential: they ask canopy-web for a fresh
8-hour token per use, because the refresh token rotates and exactly one process
may refresh it.

The open question that leaves is whose behalf a given piece of agent work is
on — obvious for a chat session, `created_by` for a schedule, unclear for
inbound mail. See
`docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`.
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

    `account_login` is what the owner picker shows and what an org-addressed
    call is aimed at; `is_org` distinguishes a personal account from an org.

    `repository_selection` and `repositories` exist so a user can VERIFY the
    grant they made. The panel used to report only the account — "access
    granted on dimagi-internal" — for an installation scoped to a single repo,
    which reads like the whole organisation and overstates the grant to the
    person reading it. Since the advice canopy gives is "pick only the repos you
    want agents in", it has to then show what was picked, or that advice is
    unverifiable.
    """

    installation_id: int
    account_login: str
    account_type: str  # "User" | "Organization"
    # "selected" | "all", straight from GitHub. The single most useful field
    # here: it answers "did I actually scope this?" without counting rows.
    repository_selection: str = "selected"
    # Full names ("owner/repo"). Empty when `repository_selection == "all"`,
    # where enumerating is pointless and potentially thousands of rows.
    repositories: tuple[str, ...] = ()
    # What GitHub reports as the total, which may exceed len(repositories) if
    # the grant is larger than one page.
    repository_count: int = 0

    @property
    def is_org(self) -> bool:
        return self.account_type == "Organization"

    @property
    def grants_all_repositories(self) -> bool:
        return self.repository_selection == "all"


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
    """The INSTALLATION flow — the one that asks which repositories to grant.

    Returns "" when the slug is unset, and the UI omits the link rather than
    rendering one that 404s.

    AUTHORIZING AND INSTALLING ARE DIFFERENT THINGS, and conflating them is the
    mistake this function exists to correct:

    - `/login/oauth/authorize` (see `begin_authorization`) asks "may this app
      act as you?". It yields identity and a token, and shows **no repository
      picker at all**.
    - `/apps/{slug}/installations/new` (this) asks "which account, and which
      repositories?". That picker is GitHub's own consent screen — we cannot
      render it, and should not be able to: a third party drawing "which repos
      do you grant?" is a phishing surface, so GitHub owns that boundary.

    A user who only authorizes ends up with a working token and access to ZERO
    repositories — which looks like success and pushes nothing. Measured on
    labs 2026-09-13: `@jjackson` authorized cleanly, the panel reported
    "connected", and `list_installations` returned 0.

    And since canopy no longer creates repositories, nothing is ever added to an
    installation automatically — GitHub only auto-grants access to repos the app
    itself created. Every repository an agent touches, including each new agent
    repo, is one the user picked on this screen.

    So this is the entry point for connecting, not a follow-up step. Because
    the app has "Request user authorization (OAuth) during installation"
    enabled, this single trip installs AND authorizes, returning a `code` to
    the callback — see `begin_connection`.
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


def begin_connection(session, *, redirect_uri: str, reauthorize: bool = False) -> str:
    """Where to send someone pressing Connect. One trip, both grants.

    Defaults to the INSTALLATION flow, because that is the only one that asks
    which repositories to grant — and with "Request user authorization (OAuth)
    during installation" enabled on the app, it authorizes in the same pass and
    returns a `code` to the callback. Sending people to the bare authorize URL
    (which this used to do) produced a connection with access to nothing.

    `reauthorize=True` picks the plain authorize flow instead, for the one case
    where it is right: an existing installation whose TOKEN went stale. Those
    users do not need the repository picker again, and `installations/new` would
    show them a configure screen rather than a clean re-authorization.

    The same session `state` is minted either way, so the callback's CSRF check
    behaves identically — `installations/new` passes `state` through, which is
    what makes that uniformity possible.
    """
    cid, _ = _require_configured()
    if reauthorize or not app_slug():
        # No slug means we cannot build an install URL at all; a plain
        # authorization is strictly better than a dead link, and the panel
        # separately reports that there are no installations.
        return begin_authorization(session, redirect_uri=redirect_uri)

    state = _secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state
    from urllib.parse import urlencode

    # `redirect_uri` is deliberately NOT sent: the installation flow returns to
    # the app's registered callback, and passing a mismatching one is an error.
    del cid
    return f"{install_url()}?{urlencode({'state': state})}"


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

    An EMPTY list is the load-bearing case, not an edge case. It means the
    person authorized without installing, so they hold a valid token and can
    reach no repository at all. The panel has to say so; treating empty as
    "fine" is what shipped first and it reported success to a user who could
    push nothing.
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
        selection = row.get("repository_selection") or "selected"
        inst_id = int(row.get("id") or 0)
        repos: tuple[str, ...] = ()
        count = 0
        if selection != "all":
            # One extra call per installation, and only for scoped grants. In
            # practice that is one or two calls; for an "all repositories"
            # grant it would be a pointless walk through everything the person
            # can see, so it is skipped and the UI says "all" instead.
            repos, count = _installation_repositories(token, inst_id)
        out.append(Installation(
            installation_id=inst_id,
            account_login=login,
            account_type=account.get("type") or "User",
            repository_selection=selection,
            repositories=repos,
            repository_count=count,
        ))
    return out


def _installation_repositories(token: str, installation_id: int) -> tuple[tuple[str, ...], int]:
    """The repositories one installation actually reaches.

    Best-effort: a failure here degrades to "no names, count 0" rather than
    failing the whole list, because knowing WHICH accounts are installed is
    more important than knowing which repos, and an installation the user can
    see is better than an error page.
    """
    try:
        resp = requests.get(
            f"{GITHUB_API}/user/installations/{installation_id}/repositories",
            headers=_auth_headers(token),
            params={"per_page": 100},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return (), 0
        body = resp.json()
    except requests.RequestException:
        return (), 0
    names = tuple(
        r.get("full_name") or "" for r in body.get("repositories", []) if r.get("full_name")
    )
    return names, int(body.get("total_count") or len(names))


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
