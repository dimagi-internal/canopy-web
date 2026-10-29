"""The two browser legs of connecting an external account (person or agent).

Bare Django views, for the reason `apps.github_connect.views` gives: redirects
and the session do not fit the Ninja contract. Login is required on both; they
are not in `PUBLIC_PATH_PREFIXES`.

`agent=<slug>` makes the connection the agent's rather than the caller's, and
needs the caller to be an admin of it. That is checked at START and again at
CALLBACK, because the callback may arrive minutes later and admin can be
revoked in between; a revoked admin must not be able to finish attaching a
login to the agent. An agent's start is a POST only (CSRF-checked): a GET link
could otherwise be used to make an admin's browser attach whatever provider
login it holds to the agent. The provider is also asked to show a login where
it supports one (`Provider.authorize_params(for_agent=True)`; Salesforce:
`prompt=login consent`), so the account is chosen, not inherited.

A person's flow is checked at callback too: the user who started it must be the
one finishing it (the session is the same browser, but it may have changed
hands between the legs).

The browser always lands back on a same-site `return_to` with
`?connection=<provider>&status=connected|error[&reason=<code>]`.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit

from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.agents.models import Agent

from . import providers, services

DEFAULT_RETURN = "/settings"


def _safe_return_to(value: str | None) -> str:
    """A same-site path, else `/settings`. Rejects absolute URLs, `//host`, and
    backslash tricks that browsers normalize into `//host`."""
    v = value or ""
    if not v.startswith("/") or v.startswith("//") or "\\" in v or any(c in v for c in "\r\n\t"):
        return DEFAULT_RETURN
    return v


def _land(request: HttpRequest, return_to: str, provider: str, status: str, reason: str = ""):
    """Redirect to `return_to` (under SCRIPT_NAME, as `github_connect` does) with
    the outcome appended, keeping any query it already had."""
    parts = urlsplit(_safe_return_to(return_to))
    params = parse_qsl(parts.query, keep_blank_values=True)
    params += [("connection", provider), ("status", status)]
    if reason:
        params.append(("reason", reason))
    url = f"{request.META.get('SCRIPT_NAME', '')}{parts.path}?{urlencode(params)}"
    if parts.fragment:
        url += f"#{parts.fragment}"
    return HttpResponseRedirect(url)


def _redirect_uri(request: HttpRequest, provider: str) -> str:
    # reverse(), not request.path: see github_connect.views._redirect_uri.
    return request.build_absolute_uri(reverse("connections_callback", args=[provider]))


def _check_provider(provider: str) -> None:
    try:
        providers.get(provider)
    except providers.UnknownProvider:
        raise Http404("unknown provider") from None


@login_required
@require_http_methods(["GET", "POST"])
def connections_start(request: HttpRequest, provider: str) -> HttpResponse:
    _check_provider(provider)
    if request.method != "POST" and "agent" in request.GET:
        return HttpResponseNotAllowed(["POST"])
    params = request.POST if request.method == "POST" else request.GET
    return_to = _safe_return_to(params.get("return_to"))
    slug = params.get("agent")
    if slug:
        agent = Agent.objects.filter(slug=slug).first()
        if agent is None:
            raise Http404("unknown agent")
        if not agent.is_admin(request.user):
            return HttpResponseForbidden("admin of that agent required")
        owner = ("agent", agent.pk)
    else:
        owner = ("user", request.user.pk)
    try:
        url = services.begin(request.session, provider=provider, owner=owner,
                             redirect_uri=_redirect_uri(request, provider), return_to=return_to)
    except services.NotConfigured:
        return _land(request, return_to, provider, "error", "not_configured")
    return HttpResponseRedirect(url)


@login_required
@require_http_methods(["GET"])
def connections_callback(request: HttpRequest, provider: str) -> HttpResponse:
    _check_provider(provider)
    state = request.GET.get("state") or ""
    parked = services.pending_for(request.session, state) if state else None
    return_to = (parked or {}).get("return_to") or DEFAULT_RETURN

    if parked is None or parked.get("provider") != provider:
        return _land(request, return_to, provider, "error", "bad_state")

    # Re-authorize: admin may have been revoked since `start`; a person's flow
    # finishes only for the person who started it.
    if parked.get("owner_kind") == "agent":
        agent = Agent.objects.filter(pk=parked.get("owner_id")).first()
        if agent is not None and not agent.is_admin(request.user):
            services.spend(request.session, state)  # nothing exchanged, nothing stored
            return _land(request, return_to, provider, "error", "forbidden")
    elif parked.get("owner_id") != request.user.pk:
        services.spend(request.session, state)
        return _land(request, return_to, provider, "error", "forbidden")

    if request.GET.get("error"):  # the user declined, or the provider refused
        services.spend(request.session, state)
        return _land(request, return_to, provider, "error", "access_denied"
                     if request.GET["error"] == "access_denied" else "provider_error")

    try:
        services.complete(request.session, provider=provider, code=request.GET.get("code") or "",
                          state=state, redirect_uri=_redirect_uri(request, provider), by=request.user)
    except services.ConnectionError as e:  # the service logs a provider failure itself
        return _land(request, return_to, provider, "error", e.code)
    return _land(request, return_to, provider, "connected")
