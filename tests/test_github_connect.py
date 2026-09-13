"""Connecting a GitHub account, and the three ways GitHub can arrive at the callback.

GitHub itself is faked at the `requests` boundary. What is worth pinning here is
not "does an HTTP client work" but the handful of decisions that are easy to get
wrong and expensive to discover live:

- a rejected grant arrives as **HTTP 200 with an `error` key**, so checking the
  status code alone reports failure as success;
- the callback is reached by three different flows, only one of which carries a
  `state`, so an unconditional CSRF check rejects the legitimate ones;
- the refresh token **rotates**, so failing to store the new one bricks the
  connection on second use;
- `installation_id` in the callback URL is attacker-supplied.

Every one of those is invisible until a real user presses the button, which is
why they are tested rather than eyeballed.
"""
from __future__ import annotations

from unittest import mock
from urllib.parse import unquote_plus

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.tokens import github_app
from apps.tokens.models import GitHubConnection

pytestmark = pytest.mark.django_db

CONFIGURED = {
    "GITHUB_APP_CLIENT_ID": "Iv23liTESTCLIENTID",
    "GITHUB_APP_CLIENT_SECRET": "shh-test-secret",
    "GITHUB_APP_SLUG": "canopy-agents",
}


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="gh", email="gh@dimagi.com")


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def configured(settings):
    for k, v in CONFIGURED.items():
        setattr(settings, k, v)
    return settings


def _token_response(**over):
    body = {
        "access_token": "ghu_access_1",
        "expires_in": 28800,
        "refresh_token": "ghr_refresh_1",
        "refresh_token_expires_in": 15811200,
        "token_type": "bearer",
    }
    body.update(over)
    return body


def _fake_post(body, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return mock.patch("apps.tokens.github_app.requests.post", return_value=resp)


def _fake_get(body, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = body
    resp.raise_for_status.return_value = None
    return mock.patch("apps.tokens.github_app.requests.get", return_value=resp)


# --- the happy path -----------------------------------------------------------

def test_connecting_stores_the_refresh_token_encrypted_and_not_the_access_token(
    client, user, configured
):
    """The access token is deliberately never persisted.

    It lives 8 hours and is re-minted on use, so storing it would add a
    credential at rest that buys nothing. A stolen row is then worth one
    refresh call to detect and revoke, rather than indefinite access.
    """
    start = client.get("/auth/github/start/")
    assert start.status_code == 302
    # The INSTALL flow, not the authorize flow — see the dedicated tests below.
    assert start["Location"].startswith("https://github.com/apps/canopy-agents/installations/new?")
    state = client.session[github_app.STATE_SESSION_KEY]

    with _fake_post(_token_response()), _fake_get({"login": "jjackson", "id": 12345}):
        res = client.get(f"/auth/github/callback/?code=abc&state={state}")

    assert res.status_code == 302
    assert "github=connected" in res["Location"]
    conn = GitHubConnection.objects.get(user=user)
    assert conn.github_login == "jjackson"
    assert conn.github_user_id == 12345
    assert decrypt_secret(conn.refresh_token_enc) == "ghr_refresh_1"
    assert conn.refresh_token_expires_at is not None
    assert not conn.needs_reconnect
    # The row holds no access token at all — not even a column for one.
    assert not any("access" in f.name for f in GitHubConnection._meta.get_fields())


# --- which GitHub screen a user is sent to ------------------------------------
# REGRESSION THIS PINS. Connect originally pointed at /login/oauth/authorize,
# which asks "may this app act as you?" and shows NO repository picker. The
# result was a connection holding a valid token with access to zero
# repositories — reported to the user as success. Measured on labs 2026-09-13:
# authorized cleanly, `list_installations` returned 0, nothing could be pushed.
# The installation flow is the only one that asks about repositories, and it
# authorizes in the same trip.


def test_connect_sends_a_new_user_to_the_INSTALL_flow(client, configured):
    res = client.get("/auth/github/start/")
    loc = res["Location"]
    assert loc.startswith("https://github.com/apps/canopy-agents/installations/new?"), loc
    # `state` still rides along — installations/new passes it through, which is
    # what lets the callback's CSRF check treat both flows identically.
    assert f"state={client.session[github_app.STATE_SESSION_KEY]}" in loc


def test_a_stale_token_on_an_existing_install_goes_to_the_AUTHORIZE_flow(client, user, configured):
    """The one case where the repository picker is the wrong screen.

    They are already installed; only the token went stale. Sending them to
    `installations/new` shows a configure screen rather than a clean
    re-authorization, so they would have no way to mint a working token again.
    """
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_dead"),
        refresh_failed_at=timezone.now(),
    )
    res = client.get("/auth/github/start/")
    assert res["Location"].startswith("https://github.com/login/oauth/authorize?")
    # And THAT flow must carry the exact registered callback. Under the
    # script-prefix stripping this deployment runs with, `request.path` comes
    # back WITHOUT `/canopy` while `reverse()` re-adds it — building it from the
    # wrong one fails the exchange with a `redirect_uri_mismatch` that reads
    # like a credential problem.
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fgithub%2Fcallback%2F" in res["Location"]


def test_a_healthy_connection_can_still_reach_the_install_flow(client, user, configured):
    """Pressing Connect again while healthy means "install somewhere else"."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_ok"),
        refresh_token_expires_at=timezone.now() + timezone.timedelta(days=180),
    )
    res = client.get("/auth/github/start/")
    assert res["Location"].startswith("https://github.com/apps/canopy-agents/installations/new?")


def test_an_install_that_returns_no_code_names_the_missing_app_setting(client, configured):
    """The failure that is unguessable from the outside.

    A fresh install arriving with no `code` means the app lacks "Request user
    authorization (OAuth) during installation". GitHub shows the app installed,
    canopy stores nothing, and the panel says "not connected" — with no way to
    tell that the cause is an app setting this code cannot read. So it is named
    rather than folded into the benign update case.
    """
    res = client.get("/auth/github/callback/?installation_id=42&setup_action=install")
    assert res.status_code == 302
    assert "github=install_without_code" in res["Location"]
    assert not GitHubConnection.objects.exists()


# --- the three arrivals at the callback ---------------------------------------

def test_a_state_less_code_is_accepted_because_github_starts_flows_too(client, user, configured):
    """Authorization-during-installation sends a `code` and no `state`.

    GitHub only echoes `state` when WE supplied it. With "Request user
    authorization (OAuth) during installation" enabled — which is how a user
    installs and authorizes in one pass — GitHub initiates the redirect itself
    and there is no state to echo. Requiring one unconditionally would reject
    the primary install path.
    """
    assert github_app.STATE_SESSION_KEY not in client.session
    with _fake_post(_token_response()), _fake_get({"login": "jjackson", "id": 1}):
        res = client.get("/auth/github/callback/?code=abc")
    assert res.status_code == 302
    assert "github=connected" in res["Location"]
    assert GitHubConnection.objects.filter(user=user).exists()


def test_a_mismatched_state_is_refused_when_one_is_pending(client, user, configured):
    """The CSRF case that matters: binding the WRONG GitHub account to a user.

    When the session has a pending state, a `state` that does not match it is
    an attacker replaying their own authorization code at a victim's session.
    That must fail even though the state-less path above is permitted — the
    difference is whether canopy had a request in flight to hijack.
    """
    client.get("/auth/github/start/")
    with _fake_post(_token_response()), _fake_get({"login": "attacker", "id": 9}):
        res = client.get("/auth/github/callback/?code=abc&state=not-the-one")
    assert res.status_code == 302
    assert "github=error" in res["Location"]
    assert not GitHubConnection.objects.filter(user=user).exists()


def test_the_state_is_single_use(client, user, configured):
    """Popped, not read: a captured callback URL must not be replayable."""
    client.get("/auth/github/start/")
    state = client.session[github_app.STATE_SESSION_KEY]
    with _fake_post(_token_response()), _fake_get({"login": "jjackson", "id": 1}):
        assert "github=connected" in client.get(
            f"/auth/github/callback/?code=abc&state={state}")["Location"]
    with _fake_post(_token_response()), _fake_get({"login": "jjackson", "id": 1}):
        second = client.get(f"/auth/github/callback/?code=abc&state={state}")
    # The session no longer holds a state, so this is treated as a
    # GitHub-initiated arrival rather than a verified one — which is the
    # documented behaviour, but the point is that the state itself is gone.
    assert github_app.STATE_SESSION_KEY not in client.session
    assert second.status_code == 302


def test_an_installation_update_with_no_code_is_not_an_error(client, user, configured):
    """The "Redirect on update" arrival: `installation_id`, `setup_action`, no code.

    Fires when someone adds or removes repositories. There is nothing to
    exchange, so the right answer is to send them back to Settings — where the
    installation list is re-read — rather than report a failed connection.
    """
    res = client.get("/auth/github/callback/?installation_id=777&setup_action=update")
    assert res.status_code == 302
    assert "github=installation_updated" in res["Location"]


def test_a_spoofed_installation_id_grants_nothing(client, user, configured):
    """GitHub's own warning: "bad actors can hit this URL with a spoofed
    `installation_id`". Nothing here reads it, so there is nothing to spoof
    into — no row is created, no access is recorded, no id is stored."""
    client.get("/auth/github/callback/?installation_id=999999&setup_action=install")
    assert not GitHubConnection.objects.exists()


# --- the failure modes that look like success ---------------------------------

def test_a_rejected_grant_arrives_as_http_200_and_is_still_a_failure(client, user, configured):
    """GitHub answers a bad code with 200 plus an `error` key.

    `raise_for_status()` is therefore not enough on its own — a status-only
    check would store an empty grant and report success.
    """
    with _fake_post({"error": "bad_verification_code",
                     "error_description": "The code passed is incorrect or expired."}):
        res = client.get("/auth/github/callback/?code=stale")
    assert "github=error" in res["Location"]
    assert not GitHubConnection.objects.exists()


def test_a_grant_with_no_refresh_token_is_refused(client, user, configured):
    """Which means the app has "Expire user authorization tokens" OFF.

    GitHub then issues a token that never expires and no refresh token at all.
    Storing it would quietly swap the reviewed risk (an 8-hour window) for a
    permanent credential holding `Administration: write`, so this refuses and
    names the setting instead.
    """
    with _fake_post(_token_response(refresh_token=None)):
        res = client.get("/auth/github/callback/?code=abc")
    assert "github=error" in res["Location"]
    # Decoded, because the detail rides in a query string: the user has to be
    # told WHICH app setting is wrong, or "connection failed" is unactionable.
    assert "Expire user authorization tokens" in unquote_plus(res["Location"])
    assert not GitHubConnection.objects.exists()


def test_github_being_unreachable_does_not_500(client, user, configured):
    """A third party being down must not present as a canopy crash."""
    with mock.patch("apps.tokens.github_app.requests.post",
                    side_effect=requests.ConnectionError("boom")):
        res = client.get("/auth/github/callback/?code=abc")
    assert res.status_code == 302
    assert "could not reach GitHub" in unquote_plus(res["Location"])


def test_an_unconfigured_deployment_says_so_instead_of_crashing(client, settings):
    """No credentials is a real state — a fresh checkout, or before the secret
    is set. `/settings` must keep working and say "not set up"."""
    settings.GITHUB_APP_CLIENT_ID = ""
    settings.GITHUB_APP_CLIENT_SECRET = ""
    res = client.get("/auth/github/start/")
    assert res.status_code == 302
    assert "github=not_configured" in res["Location"]


# --- using the grant ----------------------------------------------------------

def test_refresh_stores_the_rotated_token(user, configured):
    """GitHub returns a NEW refresh token and invalidates the old one.

    Not storing it works exactly once and then bricks the connection — the
    kind of bug that passes a manual test and fails eight hours later.
    """
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_refresh_1"),
        refresh_token_expires_at=timezone.now() + timezone.timedelta(days=180),
    )
    with _fake_post(_token_response(access_token="ghu_access_2",
                                    refresh_token="ghr_refresh_2")):
        token = github_app.access_token_for(user)
    assert token == "ghu_access_2"
    conn = GitHubConnection.objects.get(user=user)
    assert decrypt_secret(conn.refresh_token_enc) == "ghr_refresh_2"
    assert conn.last_used_at is not None


def test_a_rejected_refresh_becomes_reconnect_rather_than_an_opaque_401(user, configured):
    """Stamping the failure is what turns this into a visible state.

    Without it, a revoked authorization surfaces as a 401 in the middle of
    creating an agent — at which point the user has a half-made agent and no
    idea that the remedy is a button in Settings.
    """
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_revoked"),
    )
    with _fake_post({"error": "bad_refresh_token"}):
        with pytest.raises(github_app.GitHubAuthError):
            github_app.access_token_for(user)
    conn = GitHubConnection.objects.get(user=user)
    assert conn.refresh_failed_at is not None
    assert conn.needs_reconnect


def test_an_expired_refresh_token_needs_reconnect_without_calling_github(user, configured):
    """Six months unused. Detected locally, so the user gets the right message
    rather than a round trip that fails."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_old"),
        refresh_token_expires_at=timezone.now() - timezone.timedelta(days=1),
    )
    with mock.patch("apps.tokens.github_app.requests.post") as post:
        with pytest.raises(github_app.GitHubAuthError):
            github_app.access_token_for(user)
    post.assert_not_called()


def test_installations_come_from_github_not_from_a_cache(user, configured):
    """The owner picker must reflect an install done seconds ago in another tab."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    payload = {"installations": [
        {"id": 1, "account": {"login": "jjackson", "type": "User"}},
        {"id": 2, "account": {"login": "dimagi-internal", "type": "Organization"}},
        {"id": 3, "account": {}},  # malformed — skipped, not crashed on
    ]}
    with _fake_post(_token_response()), _fake_get(payload):
        rows = github_app.list_installations(user)
    assert [r.account_login for r in rows] == ["jjackson", "dimagi-internal"]
    assert [r.is_org for r in rows] == [False, True]


# --- the JSON surface ---------------------------------------------------------

def test_status_reports_not_configured_without_touching_github(client, settings):
    settings.GITHUB_APP_CLIENT_ID = ""
    settings.GITHUB_APP_CLIENT_SECRET = ""
    with mock.patch("apps.tokens.github_app.requests.get") as get:
        body = client.get("/api/tokens/github").json()
    get.assert_not_called()
    assert body == {"configured": False, "connected": False, "github_login": "",
                    "needs_reconnect": False, "install_url": ""}


def test_status_never_calls_github(client, user, configured):
    """It is read on every visit to /settings. A network call there would make
    an unrelated page slow, and fail, because of a third party."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    with mock.patch("apps.tokens.github_app.requests.get") as get, \
         mock.patch("apps.tokens.github_app.requests.post") as post:
        body = client.get("/api/tokens/github").json()
    get.assert_not_called()
    post.assert_not_called()
    assert body["connected"] is True
    assert body["github_login"] == "jjackson"
    assert body["install_url"] == "https://github.com/apps/canopy-agents/installations/new"


def test_disconnect_is_mine_only_and_idempotent(client, user, configured):
    other = get_user_model().objects.create_user(username="other", email="other@dimagi.com")
    GitHubConnection.objects.create(
        user=user, github_login="mine", github_user_id=1,
        refresh_token_enc=encrypt_secret("a"))
    GitHubConnection.objects.create(
        user=other, github_login="theirs", github_user_id=2,
        refresh_token_enc=encrypt_secret("b"))

    assert client.delete("/api/tokens/github").status_code == 204
    assert not GitHubConnection.objects.filter(user=user).exists()
    # Someone else's grant is untouched — there is no id in the path to get wrong.
    assert GitHubConnection.objects.filter(user=other).exists()
    # Already gone: still 204, because the caller's intent is satisfied.
    assert client.delete("/api/tokens/github").status_code == 204


def test_installations_endpoint_409s_when_the_grant_needs_reconnecting(client, user, configured):
    """409 and not 500: the remedy is a user action, not a retry."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_revoked"),
    )
    with _fake_post({"error": "bad_refresh_token"}):
        res = client.get("/api/tokens/github/installations")
    assert res.status_code == 409, res.content


def test_the_whole_surface_requires_a_login(configured):
    """Nothing here is public, and the callback least of all — its job is to
    bind a grant to an identity, so it has nothing to do without one."""
    anon = Client()
    for path in ("/api/tokens/github", "/api/tokens/github/installations"):
        assert anon.get(path).status_code in (401, 403), path


# --- what the grant actually reaches ------------------------------------------
# The panel used to report only the ACCOUNT: "access granted on dimagi-internal"
# for an installation scoped to one repository. That reads like the whole
# organisation and overstates the grant to whoever is reading it — and since
# canopy's own advice is "pick only the repos you want agents in", it has to
# then show what was picked or that advice cannot be checked.


def _sequential_get(*bodies):
    """Distinct responses per call, so the installations lookup and the
    per-installation repository lookup can be told apart."""
    resps = []
    for body in bodies:
        status = 200
        if isinstance(body, tuple):
            body, status = body
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = body
        r.raise_for_status.return_value = None
        resps.append(r)
    return mock.patch("apps.tokens.github_app.requests.get", side_effect=resps)


def test_a_scoped_installation_reports_the_repositories_it_reaches(user, configured):
    """The real shape from labs 2026-09-13: one org, one repo."""
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    installs = {"installations": [{
        "id": 161425953,
        "account": {"login": "dimagi-internal", "type": "Organization"},
        "repository_selection": "selected",
    }]}
    repos = {"total_count": 1, "repositories": [{"full_name": "dimagi-internal/ace"}]}
    with _fake_post(_token_response()), _sequential_get(installs, repos):
        rows = github_app.list_installations(user)
    assert len(rows) == 1
    assert rows[0].repository_selection == "selected"
    assert rows[0].repositories == ("dimagi-internal/ace",)
    assert rows[0].repository_count == 1
    assert rows[0].grants_all_repositories is False


def test_an_all_repositories_grant_is_flagged_and_not_enumerated(user, configured):
    """Walking every repo the account can see would be pointless and huge.

    It is also the grant the connect advice exists to steer people away from,
    so the UI needs to distinguish it rather than render a very long list.
    """
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    installs = {"installations": [{
        "id": 7,
        "account": {"login": "jjackson", "type": "User"},
        "repository_selection": "all",
    }]}
    # Exactly ONE get: the installations call. A second would mean we tried to
    # enumerate an "all" grant.
    with _fake_post(_token_response()), _sequential_get(installs) as get:
        rows = github_app.list_installations(user)
    assert get.call_count == 1
    assert rows[0].grants_all_repositories is True
    assert rows[0].repositories == ()


def test_a_failed_repository_lookup_degrades_instead_of_failing_the_list(user, configured):
    """Knowing WHICH accounts are installed matters more than which repos.

    A 500 from the per-installation call must not turn the whole panel into an
    error — the account list is still the thing the owner picker needs.
    """
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    installs = {"installations": [{
        "id": 9,
        "account": {"login": "dimagi-internal", "type": "Organization"},
        "repository_selection": "selected",
    }]}
    with _fake_post(_token_response()), _sequential_get(installs, ({}, 500)):
        rows = github_app.list_installations(user)
    assert len(rows) == 1
    assert rows[0].account_login == "dimagi-internal"
    assert rows[0].repositories == ()
    assert rows[0].repository_count == 0


def test_the_endpoint_serializes_the_repositories(client, user, configured):
    GitHubConnection.objects.create(
        user=user, github_login="jjackson", github_user_id=1,
        refresh_token_enc=encrypt_secret("ghr_1"),
    )
    installs = {"installations": [{
        "id": 161425953,
        "account": {"login": "dimagi-internal", "type": "Organization"},
        "repository_selection": "selected",
    }]}
    repos = {"total_count": 1, "repositories": [{"full_name": "dimagi-internal/ace"}]}
    with _fake_post(_token_response()), _sequential_get(installs, repos):
        body = client.get("/api/tokens/github/installations").json()
    assert body == [{
        "installation_id": 161425953,
        "account_login": "dimagi-internal",
        "account_type": "Organization",
        "is_org": True,
        "repository_selection": "selected",
        "repositories": ["dimagi-internal/ace"],
        "repository_count": 1,
    }]
