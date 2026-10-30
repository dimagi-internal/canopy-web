"""The two browser legs of connecting an external account (person or agent).

Both are per workspace: `/auth/connections/<provider>/<workspace>/start|callback/`.
The flow runs with that workspace's app (its client), the callback URL is the one
that workspace's admin registers with the provider, and a callback completes only
a flow begun for the same provider AND workspace (`bad_state` otherwise, nothing
exchanged): a code is only ever exchanged by the client it was issued to.

A person connects in a workspace they can read (another one is a 404). An agent
connects with its own workspace's app, so its start names that workspace (any
other is a 404).

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
it supports one (`Provider.authorize_params(owner_kind="agent", owner=agent)`;
Salesforce: `prompt=login consent`), so the account is chosen, not inherited.

A person's flow is checked at callback too: the user who started it must be the
one finishing it (the session is the same browser, but it may have changed
hands between the legs).

The browser always lands back on a same-site `return_to` with
`?connection=<provider>&status=connected|error[&reason=<code>]`; reasons:
`not_configured`, `bad_state`, `forbidden` (the caller no longer administers the
agent, the agent left the workspace, or the person left it), `access_denied`,
`insufficient_scope`, `wrong_account` (the provider signed in an account this
owner may not connect: not the agent's own, or outside the app's allowed
domains), `provider_error`.
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
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from apps.agents.models import Agent

from . import providers, services

DEFAULT_RETURN = "/settings"


def _safe_return_to(value: str | None) -> str:
    """A same-site path, else `/settings`. Rejects absolute URLs, `//host`, and
    backslash tricks that browsers normalize into `//host`."""
    v = value or ""
    if (
        not v.startswith("/")
        or v.startswith("//")
        or "\\" in v
        or any(c in v for c in "\r\n\t")
        # No host is allowed, so only a relative URL passes (CodeQL models this sanitizer).
        or not url_has_allowed_host_and_scheme(v, allowed_hosts=set(), require_https=True)
    ):
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
    # Already same-site by construction; re-checked on the final URL, where CodeQL
    # can see the guard (it loses _safe_return_to through urlsplit).
    if not url_has_allowed_host_and_scheme(url, allowed_hosts=set(), require_https=True):
        url = DEFAULT_RETURN
    return HttpResponseRedirect(url)


def _redirect_uri(request: HttpRequest, provider: str, workspace: str) -> str:
    # reverse(), not request.path: see github_connect.views._redirect_uri.
    return request.build_absolute_uri(reverse("connections_callback", args=[provider, workspace]))


def _check_provider(provider: str) -> None:
    try:
        providers.get_class(provider)
    except providers.UnknownProvider:
        raise Http404("unknown provider") from None


@login_required
@require_http_methods(["GET", "POST"])
def connections_start(request: HttpRequest, provider: str, workspace: str) -> HttpResponse:
    _check_provider(provider)
    if request.method != "POST" and "agent" in request.GET:
        return HttpResponseNotAllowed(["POST"])
    params = request.POST if request.method == "POST" else request.GET
    return_to = _safe_return_to(params.get("return_to"))
    slug = params.get("agent")
    if slug:
        agent = Agent.objects.filter(slug=slug).first()
        # An agent connects with its own workspace's app: any other workspace is
        # "no such agent here", whether or not the caller administers it.
        if agent is None or agent.workspace_id != workspace:
            raise Http404("unknown agent")
        if not agent.is_admin(request.user):
            return HttpResponseForbidden("admin of that agent required")
        owner = ("agent", agent.pk)
    else:
        # 404, not 403: whether a workspace exists is not told to a non-member.
        if not services.person_may_use(request.user, workspace):
            raise Http404("unknown workspace")
        owner = ("user", request.user.pk)
    try:
        app = services.app_for(workspace, provider)
        url = services.begin(request.session, app=app, owner=owner,
                             redirect_uri=_redirect_uri(request, provider, workspace), return_to=return_to)
    except services.NotConfigured:
        return _land(request, return_to, provider, "error", "not_configured")
    return HttpResponseRedirect(url)


@login_required
@require_http_methods(["GET"])
def connections_callback(request: HttpRequest, provider: str, workspace: str) -> HttpResponse:
    _check_provider(provider)
    state = request.GET.get("state") or ""
    parked = services.pending_for(request.session, state) if state else None
    return_to = (parked or {}).get("return_to") or DEFAULT_RETURN

    # A callback for another provider or workspace than the flow began with
    # (an OAuth mix-up): refused before anything else, nothing exchanged.
    if parked is None or parked.get("provider") != provider or parked.get("workspace") != workspace:
        return _land(request, return_to, provider, "error", "bad_state")

    # Re-authorize what `start` checked, since either may have changed between
    # the legs: the caller still administers the agent and the agent is still in
    # this workspace (it connects with its own workspace's app only); a person's
    # flow finishes only for the person who started it, still a member here.
    if parked.get("owner_kind") == "agent":
        agent = Agent.objects.filter(pk=parked.get("owner_id")).first()
        allowed = agent is None or (agent.is_admin(request.user) and agent.workspace_id == workspace)
    else:
        allowed = (parked.get("owner_id") == request.user.pk
                   and services.person_may_use(request.user, workspace))
    if not allowed:
        services.spend(request.session, state)  # nothing exchanged, nothing stored
        return _land(request, return_to, provider, "error", "forbidden")

    if request.GET.get("error"):  # the user declined, or the provider refused
        reason = services.authorize_refused(request.session, provider=provider, state=state,
                                            error=request.GET["error"], by=request.user)
        return _land(request, return_to, provider, "error", reason)

    try:
        services.complete(request.session, provider=provider, workspace=workspace,
                          code=request.GET.get("code") or "", state=state,
                          redirect_uri=_redirect_uri(request, provider, workspace), by=request.user)
    except services.ConnectionError as e:  # the service logs a provider failure itself
        return _land(request, return_to, provider, "error", e.code)
    return _land(request, return_to, provider, "connected")
