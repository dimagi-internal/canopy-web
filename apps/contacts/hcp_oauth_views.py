"""HTTP views for the HCP service's OAuth 2.0 flow (apps/contacts/hcp_oauth.py).

Bare Django views, not Ninja routes: the authorize step renders HTML forms, and the
token and revocation endpoints take `application/x-www-form-urlencoded` as RFC 6749
and RFC 7009 require. The issuer is `<origin>/api/hcp`, so these sit under it.
"""
from __future__ import annotations

from datetime import datetime

from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.common.script_prefix import self_full_path
from apps.common.views_debug import is_machine

from . import hcp, hcp_oauth
from . import services as contact_services


def issuer(request: HttpRequest) -> str:
    return request.build_absolute_uri("/api/hcp").rstrip("/")


class _Redirect(HttpResponseRedirect):
    """A redirect to a registered (exact-matched) URI, whatever its scheme."""

    def __init__(self, to, *args, **kwargs):
        from urllib.parse import urlsplit

        scheme = urlsplit(to).scheme
        self.allowed_schemes = ["http", "https", scheme] if scheme else ["http", "https"]
        super().__init__(to, *args, **kwargs)


def _error(request, reason: str, status: int = 400) -> HttpResponse:
    return render(request, "contacts/hcp_error.html", {"reason": reason}, status=status)


def _settings_note(person) -> list[str]:
    """What the person's own switches mean for this app — said plainly, because a
    grant never overrides them."""
    state = hcp.memory_state(person)
    rec, use = state["record"], state["use"]
    notes = []
    notes.append("It can read what you allow below only while “Agents may use what they've "
                 "learned” is available and on by default for you — "
                 + ("which it is now." if use["available"] and use["default"]
                    else "which it is NOT now, so any read will be refused until you change that "
                         "on your “What agents know about me” page."))
    notes.append("It can save or change things only while “Agents may learn about me” is available "
                 "and on by default — "
                 + ("which it is now." if rec["available"] and rec["default"]
                    else "which it is NOT now, so any write will be refused."))
    return notes


@require_http_methods(["GET", "POST"])
def authorize(request: HttpRequest) -> HttpResponse:
    """GET: the consent screen (act 1). POST step=allow|deny: the answer to act 1;
    POST step=keep: the answer to the separate keep-access question (act 2)."""
    if not request.user.is_authenticated:
        from urllib.parse import urlencode

        from django.conf import settings

        return HttpResponseRedirect(f"{settings.LOGIN_URL}?{urlencode({'next': self_full_path(request)})}")
    if is_machine(request):
        return _error(request, "Sign in to canopy in your browser to answer this.", status=403)
    ok, why = hcp_oauth.may_authorize(request.user)
    if not ok:
        return _error(request, why, status=403)
    person = contact_services.person_for(user=request.user)
    if person is None:
        return _error(request, "canopy could not find your record.", status=403)

    if request.method == "POST" and request.POST.get("step") == "keep":
        pending = hcp_oauth.take_pending(request.POST.get("nonce") or "", request.user)
        if pending is None:
            return _error(request, "That request has expired. Go back to the app and start again.")
        persistent = request.POST.get("keep") == "yes"
        grant = hcp_oauth.issue_grant(pending, person=person, user=request.user, persistent=persistent)
        code = hcp_oauth.issue_code(pending, grant)
        req = hcp_oauth.AuthorizeRequest(client=grant.hcp_client, redirect_uri=pending["redirect_uri"],
                                         scopes=pending["scopes"], state=pending["state"],
                                         code_challenge=pending["code_challenge"])
        return _Redirect(req.redirect(code=code))

    params = request.POST if request.method == "POST" else request.GET
    try:
        req = hcp_oauth.parse_authorize(params)
    except hcp_oauth.BadClientOrRedirect as e:
        return _error(request, str(e))
    except hcp_oauth.AuthorizeError as e:
        return _Redirect(e.url)

    if request.method == "GET":
        expiry_labels = {"1h": "1 hour", "4h": "4 hours", "24h": "24 hours"}
        return render(request, "contacts/hcp_consent.html", {
            "client": req.client, "rows": hcp_oauth.describe_scopes(req.scopes),
            "workspaces": hcp_oauth.workspace_sources(person),
            "expiries": [(k, expiry_labels[k]) for k in hcp_oauth.TEMPORARY_CHOICES],
            "expiry_default": hcp_oauth.TEMPORARY_DEFAULT,
            "settings_notes": _settings_note(person), "redirect_host": _host(req.redirect_uri),
            "form_action": self_full_path(request).split("?", 1)[0],
            "params": {k: params.get(k, "") for k in (
                "client_id", "redirect_uri", "response_type", "scope", "state",
                "code_challenge", "code_challenge_method")},
        })

    if request.POST.get("step") != "allow":
        # 4.1.7: declining creates nothing, and need not be logged.
        return _Redirect(req.redirect(error="access_denied", error_description="The person declined."))
    expiry = request.POST.get("expiry") or hcp_oauth.TEMPORARY_DEFAULT
    if expiry not in hcp_oauth.TEMPORARY_CHOICES:
        expiry = hcp_oauth.TEMPORARY_DEFAULT
    offered = set(hcp_oauth.workspace_sources(person))
    sources = [w for w in request.POST.getlist("workspace") if w in offered]
    nonce = hcp_oauth.stash_pending(req, user=request.user, sources=sources, expiry=expiry)
    until: datetime = timezone.now() + hcp_oauth.TEMPORARY_CHOICES[expiry]
    return render(request, "contacts/hcp_keep.html", {
        "client": req.client, "nonce": nonce, "until": until,
        "form_action": self_full_path(request).split("?", 1)[0],
    })


def _host(uri: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    return parts.netloc or f"{parts.scheme}:"


def _no_store(resp: JsonResponse) -> JsonResponse:
    resp["Cache-Control"] = "no-store"
    resp["Pragma"] = "no-cache"
    return resp


@csrf_exempt
@require_http_methods(["POST"])
def token(request: HttpRequest) -> HttpResponse:
    try:
        return _no_store(JsonResponse(hcp_oauth.exchange(request.POST)))
    except hcp_oauth.OAuthError as e:
        return _no_store(JsonResponse(e.body(), status=e.status))


@csrf_exempt
@require_http_methods(["POST"])
def revoke(request: HttpRequest) -> HttpResponse:
    try:
        hcp_oauth.client_revoke(request.POST)
    except hcp_oauth.OAuthError as e:
        return JsonResponse(e.body(), status=e.status)
    return HttpResponse(status=200)


def metadata(request: HttpRequest, rest: str = "") -> HttpResponse:
    """RFC 8414 for the HCP issuer (`<origin>/api/hcp`)."""
    return JsonResponse(hcp_oauth.authorization_server_metadata(issuer(request)))


def mcp_manifest(request: HttpRequest) -> HttpResponse:
    return JsonResponse(hcp_oauth.mcp_manifest())


def root_discovery(request: HttpRequest) -> HttpResponse:
    """Appendix C at the origin's /.well-known/hcp-configuration — the same document
    as `<issuer>/.well-known/hcp-configuration`."""
    body = {**hcp.discovery(issuer(request)), "hcp_version": hcp.HCP_VERSION}
    return JsonResponse(body, content_type="application/ld+json")
