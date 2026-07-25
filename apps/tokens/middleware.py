"""Bearer-token authentication middleware.

If the incoming request carries `Authorization: Bearer <raw>` and
`request.user` isn't already authenticated, look the PAT up and stamp
the resolved user onto the request. Downstream middleware
(`LoginRequiredMiddleware`) + Ninja's `DjangoSessionAuth` then see a
real authenticated user, identical to a session-cookie flow.

CSRF: Bearer-authenticated requests are stateless and not vulnerable to
cross-site forgery, but Django's `CsrfViewMiddleware` doesn't know that
— it only short-circuits on session cookies. We set
`request._dont_enforce_csrf_checks = True` so unsafe-method PAT
callers don't get a 403.

Ordering matters in `config/settings.MIDDLEWARE`:
  1. `django.contrib.sessions.middleware.SessionMiddleware`
  2. `django.contrib.auth.middleware.AuthenticationMiddleware`
  3. **`apps.tokens.middleware.BearerTokenAuthMiddleware`**  ← here
  4. `apps.common.middleware.LoginRequiredMiddleware`
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)


class BearerTokenAuthMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self._authenticate(request)
        return self.get_response(request)

    @staticmethod
    def _authenticate(request: HttpRequest) -> None:
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return

        raw = header[len("Bearer "):].strip()
        if not raw:
            return

        from apps.tokens.models import PersonalToken

        token = PersonalToken.lookup(raw)
        if token is not None:
            PersonalToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
            request.user = token.user
            request._dont_enforce_csrf_checks = True
            return

        from apps.tokens.models import DelegatedToken

        dtok = DelegatedToken.lookup(raw)
        if dtok is not None and dtok.user.is_active:
            request.user = dtok.user
            request._dont_enforce_csrf_checks = True
