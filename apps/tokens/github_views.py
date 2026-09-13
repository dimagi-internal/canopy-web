"""The two browser legs of the GitHub App connect flow.

Bare Django views rather than Ninja routes, for the reason the repo already
applies to `/auth/cli/authorize/`: these do redirects and touch the session,
which does not fit the Ninja request/response contract. The JSON surface
(status, disconnect, installations) is Ninja, in `api.py`.

BOTH VIEWS REQUIRE A LOGGED-IN USER, and that is the point rather than an
inconvenience — the callback's whole job is to attach a GitHub grant to a
canopy identity, so there is nothing to attach without one. They are therefore
NOT added to `PUBLIC_PATH_PREFIXES`; the default-deny login middleware covers
them, and an unauthenticated hit bounces to sign-in and returns here.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import requests
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import github_app
from .models import GitHubConnection

logger = logging.getLogger(__name__)


def _settings_url(request: HttpRequest, **params) -> str:
    """`/settings` on THIS deployment, with a status for the SPA to render.

    Built from `SCRIPT_NAME` rather than a bare "/settings" because canopy-web
    serves under `/canopy` on labs; a bare path would land on a sibling tenant.
    `/settings` is an SPA route with no Django URL name, so there is nothing to
    `reverse()`.
    """
    base = request.META.get("SCRIPT_NAME", "") + "/settings"
    return f"{base}?{urlencode(params)}" if params else base


def _redirect_uri(request: HttpRequest) -> str:
    """The exact `redirect_uri` registered on the GitHub App.

    `reverse()` is load-bearing here and `request.get_full_path()` would be
    wrong. Under the ASGI script-prefix stripping, `request.path` comes back
    WITHOUT `/canopy`, while `reverse()` re-adds it via `FORCE_SCRIPT_NAME` —
    see `apps/common/script_prefix.py`. GitHub compares this string against the
    app's registered callback, so getting it wrong fails the exchange with a
    `redirect_uri_mismatch` that reads like a credential problem.
    """
    return request.build_absolute_uri(reverse("github_connect_callback"))


@login_required
@require_http_methods(["GET", "POST"])
def github_connect_start(request: HttpRequest):
    """Send the user to GitHub. Which screen depends on what they already have.

    Default: the INSTALLATION flow, which is the only one that asks which
    repositories to grant and (because the app requests user authorization
    during installation) authorizes in the same trip. This used to send everyone
    to the bare authorize URL, which produced a connection holding a valid token
    and access to zero repositories — reported to the user as success.

    The exception is a stale TOKEN on an existing connection: those people are
    already installed and do not need the repository picker again, and
    `installations/new` would show them a configure screen rather than a clean
    re-authorization. Decided server-side from `needs_reconnect`, which is a
    local field — no GitHub call, and nothing the client gets to choose.
    """
    conn = GitHubConnection.objects.filter(user=request.user).first()
    reauthorize = conn is not None and conn.needs_reconnect
    try:
        url = github_app.begin_connection(
            request.session,
            redirect_uri=_redirect_uri(request),
            reauthorize=reauthorize,
        )
    except github_app.GitHubNotConfigured:
        return HttpResponseRedirect(_settings_url(request, github="not_configured"))
    return HttpResponseRedirect(url)


@login_required
@require_http_methods(["GET"])
def github_connect_callback(request: HttpRequest):
    """Where GitHub sends the user back. Three different arrivals land here.

    1. **We started it** — `?code=…&state=…` from the Settings button. The
       state is verified against the session.
    2. **GitHub started it** — authorization during installation, which sends a
       `code` and NO `state`. `complete_authorization` handles that explicitly;
       see its docstring for why accepting a state-less code is still safe.
    3. **An installation was UPDATED** — the "Redirect on update" setting, which
       fires when someone adds or removes repositories. This arrives with
       `installation_id` and `setup_action` and often no `code` at all, so there
       is nothing to exchange: the right response is to send them back to
       Settings, where the installation list is re-read from GitHub.

    `installation_id` is deliberately ignored. GitHub's own docs warn that "bad
    actors can hit this URL with a spoofed `installation_id`", so nothing here
    trusts it — the list is always re-queried with the user's own token.
    """
    code = request.GET.get("code") or ""
    if not code:
        # An installation arrived with no code to exchange. Two very different
        # causes, and telling them apart is worth the branch.
        if request.GET.get("setup_action") == "install":
            # A FRESH install that produced no authorization code means the app
            # is missing "Request user authorization (OAuth) during
            # installation". Everything looks like it worked — GitHub shows the
            # app installed — while canopy stores nothing and reports "not
            # connected". That is unguessable from the outside, and it depends
            # on an app setting this code cannot read, so it gets named.
            logger.warning(
                "GitHub install callback carried no code for user %s — the app is "
                "probably missing 'Request user authorization (OAuth) during installation'",
                request.user.pk,
            )
            return HttpResponseRedirect(_settings_url(request, github="install_without_code"))
        # Otherwise: an installation was UPDATED (repositories added or
        # removed), or someone hit the URL directly. Nothing to record and
        # nothing wrong.
        return HttpResponseRedirect(_settings_url(request, github="installation_updated"))

    try:
        conn = github_app.complete_authorization(
            request.user,
            code=code,
            state=request.GET.get("state"),
            session=request.session,
        )
    except github_app.GitHubNotConfigured:
        return HttpResponseRedirect(_settings_url(request, github="not_configured"))
    except github_app.GitHubAuthError as exc:
        # The message is GitHub's or ours and is safe to show — it is about the
        # grant, not about internals. Logged at warning because a run of these
        # means the app registration is wrong, not that users are misbehaving.
        logger.warning("GitHub connect failed for user %s: %s", request.user.pk, exc)
        return HttpResponseRedirect(_settings_url(request, github="error", detail=str(exc)))
    except requests.RequestException as exc:
        logger.exception("GitHub connect network failure for user %s", request.user.pk)
        return HttpResponseRedirect(
            _settings_url(request, github="error", detail=f"could not reach GitHub: {exc}")
        )

    return HttpResponseRedirect(_settings_url(request, github="connected", login=conn.github_login))
