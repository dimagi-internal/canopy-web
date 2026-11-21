"""Django admin opens only to a staff person signed in through Google.

The admin's own login form checks a stored password, a way in that skips
Google, the domain allowlist and every check `auth_adapter` makes. So the form
is gone: `/admin/login/` sends a visitor to the normal Google sign-in and back,
and the admin lets in only a browser session (never a bearer token or a session
minted from one, `views_debug.is_machine`) of an active staff user. No stored
password, whoever holds one, opens it.
"""
from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, HttpResponseRedirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.common.views_debug import is_machine


def has_permission(request: HttpRequest) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_active and user.is_staff) and not is_machine(request)


def login(request: HttpRequest, extra_context=None) -> HttpResponse:
    """Stands in for `AdminSite.login`: never a password form."""
    nxt = request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                           require_https=request.is_secure()):
        nxt = ""
    nxt = nxt or reverse("admin:index")
    if not request.user.is_authenticated:
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?{urlencode({'next': nxt})}")
    if has_permission(request):
        return HttpResponseRedirect(nxt)
    # Signed in, but not staff (or a machine session): sending them back to the
    # sign-in would only loop.
    return HttpResponseForbidden("The admin is for canopy staff, signed in through Google in a browser.")


def install(site: admin.AdminSite = admin.site) -> None:
    site.login = login
    site.has_permission = has_permission
